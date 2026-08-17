//! End-to-end integration tests: Rust host ↔ Haskell WASM plugin via trampoline.
//!
//! Loads the actual compiled WASM binary, registers mock effect handlers,
//! and verifies the full protobuf encoding/decoding pipeline:
//!
//! ```text
//! WASM guest → yield_effect / suspend → EffectRegistry → mock handler → EffectResponse → WASM guest
//! ```
//!
//! All WASM exports return `WasmResult<O>` (Done | Suspend envelope).
//! `PluginManager::call` (trampoline) is the single dispatch path.
//!
//! Requires: `just wasm-all` to build the WASM binary first.

use async_trait::async_trait;
use exomonad_core::{EffectError, EffectHandler, EffectResult, RuntimeBuilder};
use prost::Message;
use serde_json::{json, Value};
use serial_test::serial;
use std::collections::BTreeSet;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Mutex, OnceLock};

static MOCK_AGENT_CLOSE_SELF_CALLS: AtomicUsize = AtomicUsize::new(0);
static MOCK_EVENTS_NOTIFY_PARENT_CALLS: AtomicUsize = AtomicUsize::new(0);
static MOCK_LAST_SPAWN_LEAF_MODEL: OnceLock<Mutex<Option<String>>> = OnceLock::new();

fn mock_last_spawn_leaf_model() -> &'static Mutex<Option<String>> {
    MOCK_LAST_SPAWN_LEAF_MODEL.get_or_init(|| Mutex::new(None))
}

// ============================================================================
// Test Infrastructure
// ============================================================================

fn wasm_binary_bytes() -> Vec<u8> {
    let manifest = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let path = manifest.join("../../.exo/wasm/wasm-guest-devswarm.wasm");
    assert!(
        path.exists(),
        "WASM binary not found at {path:?}. Build with `just wasm-all`."
    );
    std::fs::read(&path).expect("Failed to read WASM binary")
}

async fn build_test_runtime() -> exomonad_core::Runtime {
    let wasm_bytes = wasm_binary_bytes();
    RuntimeBuilder::new()
        .with_effect_handler(MockGitHandler)
        .with_effect_handler(MockLogHandler)
        .with_effect_handler(MockAgentHandler)
        .with_effect_handler(MockFsHandler)
        .with_effect_handler(MockFilePRHandler)
        .with_effect_handler(MockMergePRHandler)
        .with_effect_handler(MockEventsHandler)
        .with_effect_handler(MockSessionHandler)
        .with_effect_handler(MockKvHandler)
        .with_effect_handler(MockGitHubHandler)
        .with_effect_handler(MockCopilotHandler)
        .with_effect_handler(MockProcessHandler)
        .with_wasm_bytes(wasm_bytes)
        .build()
        .await
        .expect("Failed to build runtime with WASM plugin")
}

/// Helper: call a tool and return the MCPCallOutput JSON.
async fn call_tool(
    runtime: &exomonad_core::Runtime,
    role: &str,
    tool_name: &str,
    tool_args: Value,
) -> Value {
    let input = json!({
        "role": role,
        "toolName": tool_name,
        "toolArgs": tool_args
    });
    runtime
        .plugin_manager()
        .call("handle_mcp_call", &input)
        .await
        .unwrap_or_else(|e| panic!("handle_mcp_call failed for {tool_name}: {e}"))
}

/// Helper: assert a tool call succeeded.
fn assert_tool_success(output: &Value, tool_name: &str) {
    assert_eq!(
        output["success"], true,
        "{tool_name} should succeed: {output:#}"
    );
}

/// Helper: assert a tool call failed.
fn assert_tool_error(output: &Value, tool_name: &str) {
    assert_eq!(
        output["success"], false,
        "{tool_name} should fail: {output:#}"
    );
}

async fn list_tool_names(runtime: &exomonad_core::Runtime, role: &str) -> BTreeSet<String> {
    let tools: Vec<Value> = runtime
        .plugin_manager()
        .call("handle_list_tools", &json!({"role": role}))
        .await
        .unwrap_or_else(|e| panic!("handle_list_tools failed for {role}: {e}"));

    tools
        .iter()
        .filter_map(|tool| tool["name"].as_str().map(str::to_string))
        .collect()
}

fn missing_protocol_tools(protocol: &str, tool_names: &BTreeSet<String>) -> Vec<String> {
    tool_names
        .iter()
        .filter(|tool| !protocol.contains(&format!("- {tool}:")))
        .cloned()
        .collect()
}

#[test]
fn protocol_tool_guard_reports_a_missing_tool() {
    let mut tools = BTreeSet::new();
    tools.insert("present_tool".to_string());
    tools.insert("missing_tool".to_string());

    assert_eq!(
        missing_protocol_tools("- present_tool: available", &tools),
        vec!["missing_tool"]
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn runtime_protocols_list_every_wasm_granted_mcp_tool() {
    let runtime = build_test_runtime().await;

    for (label, role, protocol) in [
        (
            "OpenCode dev",
            "dev",
            exomonad_core::services::agent_control::OPENCODE_DEV_INSTRUCTIONS,
        ),
        (
            "Codex dev",
            "dev",
            exomonad_core::services::agent_control::CODEX_DEV_INSTRUCTIONS,
        ),
        (
            "OpenCode worker",
            "worker",
            exomonad_core::services::agent_control::OPENCODE_WORKER_INSTRUCTIONS,
        ),
        (
            "Codex worker",
            "worker",
            exomonad_core::services::agent_control::CODEX_WORKER_INSTRUCTIONS,
        ),
        (
            "Codex reviewer",
            "reviewer",
            exomonad_core::services::agent_control::CODEX_REVIEWER_INSTRUCTIONS,
        ),
    ] {
        let tools = list_tool_names(&runtime, role).await;
        let missing = missing_protocol_tools(protocol, &tools);
        assert!(
            missing.is_empty(),
            "{label} protocol does not mention WASM-granted MCP tools: {missing:?}"
        );
    }
}

fn assert_tools_present(role: &str, names: &BTreeSet<String>, expected: &[&str]) {
    for tool in expected {
        assert!(
            names.contains(*tool),
            "{role} role missing tool '{tool}'. Got: {names:?}"
        );
    }
}

fn expand_matrix_tool_cell(cell: &str) -> BTreeSet<String> {
    let mut tools = BTreeSet::new();
    let mut prefix = String::new();

    for raw_part in cell.split('/') {
        let tool = raw_part.trim().trim_matches('`');
        if tool.is_empty() {
            continue;
        }

        if let Some(suffix) = tool.strip_prefix('_') {
            if !prefix.is_empty() {
                tools.insert(format!("{prefix}_{suffix}"));
            }
            continue;
        }

        if let Some((base_prefix, _)) = tool.rsplit_once('_') {
            prefix = base_prefix.to_string();
        } else {
            prefix.clear();
        }
        tools.insert(tool.to_string());
    }

    tools
}

fn expected_tool_matrix_from_architecture_doc(
) -> std::collections::BTreeMap<String, BTreeSet<String>> {
    let doc = include_str!("../../../docs/architecture/agent-system.md");
    let mut matrix = std::collections::BTreeMap::from([
        ("root".to_string(), BTreeSet::new()),
        ("tl".to_string(), BTreeSet::new()),
        ("dev".to_string(), BTreeSet::new()),
        ("reviewer".to_string(), BTreeSet::new()),
        ("worker".to_string(), BTreeSet::new()),
    ]);
    let roles = ["root", "tl", "dev", "reviewer", "worker"];
    let mut in_tool_table = false;

    for line in doc.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with("| Tool |") || trimmed.starts_with("| Chainlink tool |") {
            in_tool_table = true;
            continue;
        }
        if in_tool_table && !trimmed.starts_with('|') {
            in_tool_table = false;
            continue;
        }
        if !in_tool_table
            || trimmed.starts_with("|------")
            || trimmed.starts_with("|---------------")
        {
            continue;
        }

        let cells: Vec<&str> = trimmed
            .trim_matches('|')
            .split('|')
            .map(str::trim)
            .collect();
        if cells.len() != 6 {
            continue;
        }

        let expanded_tools = expand_matrix_tool_cell(cells[0]);
        for (idx, role) in roles.iter().enumerate() {
            if cells[idx + 1] == "x" {
                matrix
                    .get_mut(*role)
                    .expect("role initialized")
                    .extend(expanded_tools.iter().cloned());
            }
        }
    }

    matrix
}

fn markdown_tool_visibility_diff(
    rows: &[(String, String, BTreeSet<String>, BTreeSet<String>)],
) -> String {
    let mut output = String::from(
        "\n| Runtime | Role | Missing | Unexpected |\n|---------|------|---------|------------|\n",
    );
    for (runtime, role, missing, unexpected) in rows {
        let missing = if missing.is_empty() {
            "-".to_string()
        } else {
            missing.iter().cloned().collect::<Vec<_>>().join(", ")
        };
        let unexpected = if unexpected.is_empty() {
            "-".to_string()
        } else {
            unexpected.iter().cloned().collect::<Vec<_>>().join(", ")
        };
        output.push_str(&format!(
            "| {runtime} | {role} | {missing} | {unexpected} |\n"
        ));
    }
    output
}

fn assert_tools_absent(role: &str, names: &BTreeSet<String>, forbidden: &[&str]) {
    for tool in forbidden {
        assert!(
            !names.contains(*tool),
            "{role} role should not expose tool '{tool}'. Got: {names:?}"
        );
    }
}

// ============================================================================
// Mock Effect Handlers
// ============================================================================

struct MockGitHandler;

#[async_trait]
impl EffectHandler for MockGitHandler {
    fn namespace(&self) -> &str {
        "git"
    }

    async fn handle(
        &self,
        effect_type: &str,
        payload: &[u8],
        _ctx: &exomonad_core::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        use exomonad_proto::effects::git::*;

        match effect_type {
            "git.get_branch" => {
                let _req = GetBranchRequest::decode(payload)
                    .map_err(|e| EffectError::invalid_input(format!("decode: {e}")))?;
                Ok(GetBranchResponse {
                    branch: "mock-main".into(),
                    detached: false,
                }
                .encode_to_vec())
            }
            "git.get_status" => {
                let _req = GetStatusRequest::decode(payload)
                    .map_err(|e| EffectError::invalid_input(format!("decode: {e}")))?;
                Ok(GetStatusResponse {
                    dirty_files: vec![],
                    staged_files: vec![],
                    untracked_files: vec![],
                }
                .encode_to_vec())
            }
            "git.get_recent_commits" => {
                let _req = GetCommitsRequest::decode(payload)
                    .map_err(|e| EffectError::invalid_input(format!("decode: {e}")))?;
                Ok(GetCommitsResponse { commits: vec![] }.encode_to_vec())
            }
            "git.has_unpushed_commits" => {
                let _req = HasUnpushedCommitsRequest::decode(payload)
                    .map_err(|e| EffectError::invalid_input(format!("decode: {e}")))?;
                Ok(HasUnpushedCommitsResponse {
                    has_unpushed: false,
                    count: 0,
                }
                .encode_to_vec())
            }
            "git.get_remote_url" => {
                let _req = GetRemoteUrlRequest::decode(payload)
                    .map_err(|e| EffectError::invalid_input(format!("decode: {e}")))?;
                Ok(GetRemoteUrlResponse {
                    url: "https://github.com/test/test.git".into(),
                }
                .encode_to_vec())
            }
            "git.get_repo_info" => {
                let _req = GetRepoInfoRequest::decode(payload)
                    .map_err(|e| EffectError::invalid_input(format!("decode: {e}")))?;
                Ok(GetRepoInfoResponse {
                    branch: "main".into(),
                    owner: "test-owner".into(),
                    name: "test-repo".into(),
                }
                .encode_to_vec())
            }
            _ => Err(EffectError::not_found(format!("mock_git/{effect_type}"))),
        }
    }
}

struct MockLogHandler;

#[async_trait]
impl EffectHandler for MockLogHandler {
    fn namespace(&self) -> &str {
        "log"
    }

    async fn handle(
        &self,
        effect_type: &str,
        _payload: &[u8],
        _ctx: &exomonad_core::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        use exomonad_proto::effects::log::*;

        match effect_type {
            "log.log" | "log.debug" | "log.info" | "log.warn" | "log.error" => {
                Ok(LogResponse { success: true }.encode_to_vec())
            }
            "log.emit_event" => Ok(EmitEventResponse {
                event_id: "mock-evt-1".into(),
            }
            .encode_to_vec()),
            _ => Err(EffectError::not_found(format!("mock_log/{effect_type}"))),
        }
    }
}

struct MockAgentHandler;

#[async_trait]
impl EffectHandler for MockAgentHandler {
    fn namespace(&self) -> &str {
        "agent"
    }

    async fn handle(
        &self,
        effect_type: &str,
        payload: &[u8],
        _ctx: &exomonad_core::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        use exomonad_proto::effects::agent::*;

        match effect_type {
            "agent.spawn_subtree" => {
                let agent = AgentInfo {
                    id: "test-subtree-claude".into(),
                    issue: String::new(),
                    worktree_path: "/tmp/test-worktree".into(),
                    branch_name: "main.test-subtree".into(),
                    agent_type: 1,
                    role: 1,
                    alive: true,
                    mux_window: "test-subtree".into(),
                    error: String::new(),
                    pr_number: 0,
                    pr_url: String::new(),
                    topology: 1,
                    pane_id: String::new(),
                    ..Default::default()
                };
                Ok(SpawnSubtreeResponse { agent: Some(agent) }.encode_to_vec())
            }
            "agent.spawn_leaf_subtree" => {
                let req = SpawnLeafSubtreeRequest::decode(payload)
                    .expect("mock agent handler should decode spawn_leaf_subtree request");
                *mock_last_spawn_leaf_model().lock().unwrap() =
                    (!req.model.is_empty()).then_some(req.model.clone());
                let agent = AgentInfo {
                    id: "test-leaf-codex".into(),
                    issue: String::new(),
                    worktree_path: "/tmp/test-leaf-worktree".into(),
                    branch_name: "main.test-leaf".into(),
                    agent_type: req.agent_type,
                    role: 2,
                    alive: true,
                    mux_window: "test-leaf".into(),
                    error: String::new(),
                    pr_number: 0,
                    pr_url: String::new(),
                    topology: 1,
                    pane_id: String::new(),
                    ..Default::default()
                };
                let invocation = (req.resume_pr_number != 0).then(|| InvocationHandoff {
                    invocation_id: "invocation-test-1".into(),
                    trigger: "resume_pr".into(),
                    runtime: "codex".into(),
                    branch_name: "main.test-leaf".into(),
                    target_type: "window".into(),
                    target_id: "@42".into(),
                    fresh: true,
                    ready: true,
                    outcome: "started".into(),
                });
                Ok(SpawnLeafSubtreeResponse {
                    agent: Some(agent),
                    invocation,
                }
                .encode_to_vec())
            }
            "agent.spawn_worker" => {
                let req = SpawnWorkerRequest::decode(payload)
                    .map_err(|e| EffectError::invalid_input(format!("decode: {e}")))?;
                let agent = AgentInfo {
                    id: "test-worker-codex".into(),
                    issue: String::new(),
                    worktree_path: String::new(),
                    branch_name: String::new(),
                    agent_type: req.agent_type,
                    role: 0,
                    alive: true,
                    mux_window: "test-worker".into(),
                    error: String::new(),
                    pr_number: 0,
                    pr_url: String::new(),
                    topology: 2,
                    pane_id: "%42".into(),
                    ..Default::default()
                };
                Ok(SpawnWorkerResponse { agent: Some(agent) }.encode_to_vec())
            }
            "agent.close_self" => {
                MOCK_AGENT_CLOSE_SELF_CALLS.fetch_add(1, Ordering::SeqCst);
                Ok(CloseSelfResponse {
                    success: true,
                    error: String::new(),
                }
                .encode_to_vec())
            }
            "agent.cleanup_merged" => Ok(CleanupMergedResponse {
                cleaned: vec![],
                skipped: vec![],
                errors: vec![],
            }
            .encode_to_vec()),
            "agent.restart_review" => Ok(RestartReviewResponse {
                success: true,
                error: String::new(),
                pr_number: 42,
                cleaned_reviewers: vec!["review-pr-42-codex".into()],
                runtime_state_found: true,
                watcher_state_found: true,
                legacy_review_file_removed: true,
            }
            .encode_to_vec()),
            "agent.replace_closed_pr" => {
                let req = ReplaceClosedPrRequest::decode(payload)
                    .expect("mock agent handler should decode replace_closed_pr request");
                Ok(ReplaceClosedPrResponse {
                    success: true,
                    chainlink_issue_id: req.chainlink_issue_id,
                    old_pr_number: req.closed_pr_number,
                    old_pr_state: "closed".into(),
                    old_pr_merged: false,
                    old_head_branch: "main.old-leaf-codex".into(),
                    source_head_sha: "abc123".into(),
                    original_base_branch: "main".into(),
                    old_leaf_name: req.old_leaf_name,
                    new_leaf_name: req.new_leaf_name,
                    new_branch: "main.fresh-leaf-codex".into(),
                    worktree_path: "/tmp/fresh-leaf-codex".into(),
                    spawn_status: "spawned".into(),
                    next_action: "Wait for the fresh leaf to file a new PR".into(),
                    ..Default::default()
                }
                .encode_to_vec())
            }
            "agent.watcher_pr_state" => {
                let req = WatcherPrStateRequest::decode(payload)
                    .expect("mock agent handler should decode watcher_pr_state request");
                let (ci_status, review_state) = match req.pr_number {
                    44 => ("failure", "approved"),
                    45 => ("success", "changes_requested"),
                    46 => ("pending", "approved"),
                    47 => ("unknown", "approved"),
                    48 => ("success", "commented"),
                    _ => ("success", "approved"),
                };
                let head_sha = if req.pr_number == 43 || req.pr_number == 45 || req.pr_number == 48
                {
                    "abc-new"
                } else {
                    "abc123"
                };
                Ok(WatcherPrStateResponse {
                    success: true,
                    error: String::new(),
                    pr_number: req.pr_number,
                    found: true,
                    review_state: review_state.into(),
                    ci_status: ci_status.into(),
                    head_sha: head_sha.into(),
                    head_branch: "main.test-leaf".into(),
                    base_branch: "main".into(),
                    base_sha: "base-sha".into(),
                    patch_digest: "patch-digest".into(),
                    merge_tree_sha: "merge-tree-sha".into(),
                    pr_state: "open".into(),
                    merged: false,
                    review_count: 1,
                }
                .encode_to_vec())
            }
            _ => Err(EffectError::not_found(format!("mock_agent/{effect_type}"))),
        }
    }
}

struct MockFsHandler;

#[async_trait]
impl EffectHandler for MockFsHandler {
    fn namespace(&self) -> &str {
        "fs"
    }

    async fn handle(
        &self,
        effect_type: &str,
        _payload: &[u8],
        _ctx: &exomonad_core::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        use exomonad_proto::effects::fs::*;

        match effect_type {
            "fs.read_file" => Ok(ReadFileResponse {
                content: "mock file content".into(),
                bytes_read: 17,
                truncated: false,
                total_size: 17,
            }
            .encode_to_vec()),
            _ => Err(EffectError::not_found(format!("mock_fs/{effect_type}"))),
        }
    }
}

struct MockFilePRHandler;

#[async_trait]
impl EffectHandler for MockFilePRHandler {
    fn namespace(&self) -> &str {
        "file_pr"
    }

    async fn handle(
        &self,
        effect_type: &str,
        _payload: &[u8],
        _ctx: &exomonad_core::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        use exomonad_proto::effects::file_pr::*;

        match effect_type {
            "file_pr.file_pr" => Ok(FilePrResponse {
                pr_url: "https://github.com/test/test/pull/42".into(),
                pr_number: 42,
                head_branch: "mock-main".into(),
                base_branch: "main".into(),
                created: true,
            }
            .encode_to_vec()),
            _ => Err(EffectError::not_found(format!(
                "mock_file_pr/{effect_type}"
            ))),
        }
    }
}

struct MockMergePRHandler;

#[async_trait]
impl EffectHandler for MockMergePRHandler {
    fn namespace(&self) -> &str {
        "merge_pr"
    }

    async fn handle(
        &self,
        effect_type: &str,
        _payload: &[u8],
        _ctx: &exomonad_core::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        use exomonad_proto::effects::merge_pr::*;

        match effect_type {
            "merge_pr.merge_pr" => Ok(MergePrResponse {
                success: true,
                message: "Merged PR #42".into(),
                git_fetched: true,
                branch_name: String::new(),
                head_sha: String::new(),
            }
            .encode_to_vec()),
            _ => Err(EffectError::not_found(format!(
                "mock_merge_pr/{effect_type}"
            ))),
        }
    }
}

struct MockEventsHandler;

#[async_trait]
impl EffectHandler for MockEventsHandler {
    fn namespace(&self) -> &str {
        "events"
    }

    async fn handle(
        &self,
        effect_type: &str,
        _payload: &[u8],
        _ctx: &exomonad_core::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        use exomonad_proto::effects::events::*;

        match effect_type {
            "events.notify_parent" => {
                MOCK_EVENTS_NOTIFY_PARENT_CALLS.fetch_add(1, Ordering::SeqCst);
                Ok(NotifyParentResponse { ack: true }.encode_to_vec())
            }
            "events.notify_event" => Ok(NotifyEventResponse { success: true }.encode_to_vec()),
            _ => Err(EffectError::not_found(format!("mock_events/{effect_type}"))),
        }
    }
}

struct MockSessionHandler;

#[async_trait]
impl EffectHandler for MockSessionHandler {
    fn namespace(&self) -> &str {
        "session"
    }

    async fn handle(
        &self,
        effect_type: &str,
        _payload: &[u8],
        _ctx: &exomonad_core::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        use exomonad_proto::effects::session::*;

        match effect_type {
            "session.register_claude_id" => {
                Ok(RegisterClaudeSessionResponse { success: true }.encode_to_vec())
            }
            "session.register_team" => Ok(RegisterTeamResponse { success: true }.encode_to_vec()),
            "session.list_agents" => Ok(ListAgentsResponse {
                agents: vec![AgentStatus {
                    name: "worker-a".into(),
                    role: "worker".into(),
                    issue: "7".into(),
                    window_id: String::new(),
                    pane_id: "%3".into(),
                    window_alive: true,
                    age_mins: 12,
                    birth_branch: "main.worker-a".into(),
                    lifecycle_status: "LIVE".into(),
                }],
            }
            .encode_to_vec()),
            _ => Err(EffectError::not_found(format!(
                "mock_session/{effect_type}"
            ))),
        }
    }
}

struct MockKvHandler;

#[async_trait]
impl EffectHandler for MockKvHandler {
    fn namespace(&self) -> &str {
        "kv"
    }

    async fn handle(
        &self,
        effect_type: &str,
        _payload: &[u8],
        _ctx: &exomonad_core::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        use exomonad_proto::effects::kv::*;

        match effect_type {
            "kv.get" => Ok(GetResponse {
                found: false,
                value: String::new(),
            }
            .encode_to_vec()),
            "kv.set" => Ok(SetResponse { success: true }.encode_to_vec()),
            _ => Err(EffectError::not_found(format!("mock_kv/{effect_type}"))),
        }
    }
}

struct MockGitHubHandler;

#[async_trait]
impl EffectHandler for MockGitHubHandler {
    fn namespace(&self) -> &str {
        "github"
    }

    async fn handle(
        &self,
        effect_type: &str,
        payload: &[u8],
        _ctx: &exomonad_core::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        use exomonad_proto::effects::github::*;

        match effect_type {
            "github.list_issues" => Ok(ListIssuesResponse { issues: vec![] }.encode_to_vec()),
            "github.get_issue" => Ok(GetIssueResponse {
                issue: None,
                comments: vec![],
            }
            .encode_to_vec()),
            "github.list_pull_requests" => Ok(ListPullRequestsResponse {
                pull_requests: vec![],
            }
            .encode_to_vec()),
            "github.get_pull_request" => {
                let req = GetPullRequestRequest::decode(payload)
                    .expect("mock GitHub handler should decode get_pull_request request");
                let (head_sha, review_state, review_sha) = match req.number {
                    43 => ("abc-new", ReviewState::Approved, "abc-old"),
                    45 => ("abc-new", ReviewState::ChangesRequested, "abc-old"),
                    48 => ("abc-new", ReviewState::Commented, "abc-old"),
                    _ => ("abc123", ReviewState::Approved, "abc123"),
                };
                Ok(GetPullRequestResponse {
                    pull_request: Some(PullRequest {
                        number: req.number,
                        state: IssueState::Open as i32,
                        head_ref: "main.test-leaf".into(),
                        base_ref: "main".into(),
                        head_sha: head_sha.into(),
                        ..Default::default()
                    }),
                    reviews: vec![Review {
                        state: review_state as i32,
                        commit_id: review_sha.into(),
                        ..Default::default()
                    }],
                }
                .encode_to_vec())
            }
            "github.create_pull_request" => Ok(CreatePullRequestResponse {
                pull_request: None,
                url: "https://github.com/test/test/pull/99".into(),
            }
            .encode_to_vec()),
            "github.get_pull_request_review_comments" => {
                Ok(GetPullRequestReviewCommentsResponse { comments: vec![] }.encode_to_vec())
            }
            _ => Err(EffectError::not_found(format!("mock_github/{effect_type}"))),
        }
    }
}

struct MockProcessHandler;

#[async_trait]
impl EffectHandler for MockProcessHandler {
    fn namespace(&self) -> &str {
        "process"
    }

    async fn handle(
        &self,
        effect_type: &str,
        payload: &[u8],
        _ctx: &exomonad_core::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        use exomonad_proto::effects::process::*;

        match effect_type {
            "process.run" => {
                let req = RunRequest::decode(payload).map_err(|error| {
                    EffectError::custom("decode", format!("decode RunRequest: {error}"))
                })?;
                let stdout = match req.args.as_slice() {
                    [session, status, json_flag]
                        if session == "session" && status == "status" && json_flag == "--json" =>
                    {
                        r#"{"session_id":51,"duration_minutes":3,"active_issue":{"id":7,"title":"Worker issue"}}"#
                    }
                    [issue, show, id, json_flag]
                        if issue == "issue"
                            && show == "show"
                            && id == "7"
                            && json_flag == "--json" =>
                    {
                        r#"{"id":7,"title":"Worker issue","status":"open","priority":"high","labels":["feature"]}"#
                    }
                    _ => "{}",
                };
                Ok(RunResponse {
                    exit_code: 0,
                    stdout: stdout.into(),
                    stderr: String::new(),
                }
                .encode_to_vec())
            }
            _ => Err(EffectError::not_found(format!(
                "mock_process/{effect_type}"
            ))),
        }
    }
}

struct MockCopilotHandler;

#[async_trait]
impl EffectHandler for MockCopilotHandler {
    fn namespace(&self) -> &str {
        "copilot"
    }

    async fn handle(
        &self,
        effect_type: &str,
        _payload: &[u8],
        _ctx: &exomonad_core::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        use exomonad_proto::effects::copilot::*;

        match effect_type {
            "copilot.wait_for_copilot_review" => Ok(WaitForCopilotReviewResponse {
                status: "found".into(),
                comments: vec![],
            }
            .encode_to_vec()),
            _ => Err(EffectError::not_found(format!(
                "mock_copilot/{effect_type}"
            ))),
        }
    }
}

// ============================================================================
// Tool Listing Tests (per role)
// ============================================================================

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn mcp_tool_visibility_matrix_matches_live_wasm_tools() {
    let runtime = build_test_runtime().await;
    let expected = expected_tool_matrix_from_architecture_doc();
    let runtimes = ["claude", "codex", "opencode", "codex"];
    let mut failures = Vec::new();

    for runtime_name in runtimes {
        for (role, expected_tools) in &expected {
            let actual_tools = list_tool_names(&runtime, role).await;
            let missing: BTreeSet<String> =
                expected_tools.difference(&actual_tools).cloned().collect();
            let unexpected: BTreeSet<String> =
                actual_tools.difference(expected_tools).cloned().collect();

            if !missing.is_empty() || !unexpected.is_empty() {
                failures.push((runtime_name.to_string(), role.clone(), missing, unexpected));
            }
        }
    }

    assert!(
        failures.is_empty(),
        "Live MCP tool visibility differs from docs/architecture/agent-system.md:{}",
        markdown_tool_visibility_diff(&failures)
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_tl_tools_include_spawn_and_merge() {
    let runtime = build_test_runtime().await;

    let tools: Vec<Value> = runtime
        .plugin_manager()
        .call("handle_list_tools", &json!({"role": "tl"}))
        .await
        .expect("handle_list_tools failed");

    let names: Vec<&str> = tools.iter().filter_map(|t| t["name"].as_str()).collect();

    for expected in [
        "spawn_leaf",
        "spawn_worker",
        "merge_pr",
        "file_pr",
        "notify_parent",
        "cleanup_reviewer_leaf",
        "restart_review",
        "replace_close_pr",
        "watcher_pr_state",
    ] {
        assert!(
            names.contains(&expected),
            "TL role missing tool '{expected}'. Got: {names:?}"
        );
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_replace_closed_pr_roundtrip_preserves_replacement_context() {
    let runtime = build_test_runtime().await;

    let output = call_tool(
        &runtime,
        "tl",
        "replace_close_pr",
        json!({
            "chainlink_issue_id": 541,
            "closed_pr_number": 7,
            "old_leaf_name": "old-leaf-codex",
            "new_leaf_name": "fresh-leaf",
            "replacement_task": "Address the remaining review findings",
            "operator_context": "Human approved replacement after the old PR was closed",
            "human_approved": true
        }),
    )
    .await;

    assert_tool_success(&output, "replace_close_pr");
    assert_eq!(output["result"]["success"], true);
    assert_eq!(output["result"]["chainlink_issue_id"], 541);
    assert_eq!(output["result"]["old_pr_number"], 7);
    assert_eq!(output["result"]["source_head_sha"], "abc123");
    assert_eq!(output["result"]["original_base_branch"], "main");
    assert_eq!(output["result"]["new_leaf_name"], "fresh-leaf");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_dev_tools_include_file_pr() {
    let runtime = build_test_runtime().await;

    let tools: Vec<Value> = runtime
        .plugin_manager()
        .call("handle_list_tools", &json!({"role": "dev"}))
        .await
        .expect("handle_list_tools failed for dev");

    let names: Vec<&str> = tools.iter().filter_map(|t| t["name"].as_str()).collect();

    assert!(
        names.contains(&"file_pr"),
        "Dev role missing file_pr. Got: {names:?}"
    );
    assert!(
        names.contains(&"notify_parent"),
        "Dev role missing notify_parent. Got: {names:?}"
    );

    // Dev should NOT have spawn or merge tools
    assert!(
        !names.contains(&"merge_pr"),
        "Dev role should not have merge_pr"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_worker_tools_include_notify_parent() {
    let runtime = build_test_runtime().await;

    let tools: Vec<Value> = runtime
        .plugin_manager()
        .call("handle_list_tools", &json!({"role": "worker"}))
        .await
        .expect("handle_list_tools failed for worker");

    let names: Vec<&str> = tools.iter().filter_map(|t| t["name"].as_str()).collect();

    assert!(
        names.contains(&"notify_parent"),
        "Worker role missing notify_parent. Got: {names:?}"
    );

    // Worker should have minimal tools
    assert!(
        !names.contains(&"file_pr"),
        "Worker should not have file_pr"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_reviewer_tools_include_review_commands() {
    let runtime = build_test_runtime().await;

    let tools: Vec<Value> = runtime
        .plugin_manager()
        .call("handle_list_tools", &json!({"role": "reviewer"}))
        .await
        .expect("handle_list_tools failed for reviewer");

    let names: Vec<&str> = tools.iter().filter_map(|t| t["name"].as_str()).collect();

    for expected in ["approve_pr", "request_changes", "post_review_comment"] {
        assert!(
            names.contains(&expected),
            "Reviewer role missing tool '{expected}'. Got: {names:?}"
        );
    }

    assert!(
        !names.contains(&"notify_parent"),
        "Reviewer should not have notify_parent (#268: ephemeral reviewer, no parent messaging)"
    );
    assert!(
        !names.contains(&"file_pr"),
        "Reviewer should not have file_pr"
    );
    assert!(
        !names.contains(&"merge_pr"),
        "Reviewer should not have merge_pr"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_chainlink_tools_are_scoped_by_role() {
    let runtime = build_test_runtime().await;

    let all_chainlink_tools = [
        "chainlink_issue_create",
        "chainlink_session_start",
        "chainlink_session_status",
        "chainlink_issue_show",
        "chainlink_issue_comment",
        "chainlink_subissue_create",
        "chainlink_session_work",
        "chainlink_session_end",
        "chainlink_issue_close",
        "chainlink_subissue_close",
        "chainlink_timer_start",
        "chainlink_timer_stop",
        "chainlink_timer_status",
        "chainlink_issue_list",
        "chainlink_issue_update",
        "chainlink_issue_block",
        "chainlink_issue_relate",
        "chainlink_issue_cascade",
        "chainlink_milestone_create",
        "chainlink_milestone_list",
    ];
    let dropped_chainlink_tools = [
        "chainlink_agent_init",
        "chainlink_sync",
        "chainlink_worker_status",
    ];

    let coordinator_cleanup_tools = [
        "close_issue_and_cleanup",
        "cleanup_reviewer_leaf",
        "restart_review",
        "replace_close_pr",
        "watcher_pr_state",
    ];

    let coordinator_chainlink_tools = [
        "chainlink_issue_create",
        "chainlink_session_start",
        "chainlink_session_status",
        "chainlink_issue_show",
        "chainlink_issue_comment",
        "chainlink_subissue_create",
        "chainlink_session_work",
        "chainlink_session_end",
        "chainlink_issue_close",
        "chainlink_timer_start",
        "chainlink_timer_stop",
        "chainlink_timer_status",
        "chainlink_issue_list",
        "chainlink_issue_update",
        "chainlink_issue_block",
        "chainlink_issue_relate",
        "chainlink_issue_cascade",
        "chainlink_milestone_create",
        "chainlink_milestone_list",
    ];

    let tl_tools = list_tool_names(&runtime, "tl").await;
    assert_tools_present("tl", &tl_tools, &coordinator_chainlink_tools);
    assert_tools_present("tl", &tl_tools, &coordinator_cleanup_tools);
    assert_tools_absent("tl", &tl_tools, &dropped_chainlink_tools);

    let root_tools = list_tool_names(&runtime, "root").await;
    assert_tools_present("root", &root_tools, &coordinator_chainlink_tools);
    assert_tools_present("root", &root_tools, &coordinator_cleanup_tools);
    assert_tools_absent("root", &root_tools, &dropped_chainlink_tools);

    let dev_tools = list_tool_names(&runtime, "dev").await;
    assert_tools_present(
        "dev",
        &dev_tools,
        &[
            "chainlink_session_start",
            "chainlink_session_status",
            "chainlink_issue_show",
            "chainlink_issue_comment",
            "chainlink_subissue_create",
            "chainlink_session_work",
            "chainlink_session_end",
            "chainlink_subissue_close",
        ],
    );
    assert_tools_absent("dev", &dev_tools, &coordinator_cleanup_tools);
    assert_tools_absent("dev", &dev_tools, &dropped_chainlink_tools);
    assert_tools_absent(
        "dev",
        &dev_tools,
        &[
            "chainlink_issue_create",
            "chainlink_issue_close",
            "chainlink_timer_start",
            "chainlink_timer_stop",
            "chainlink_timer_status",
            "chainlink_issue_list",
            "chainlink_issue_update",
            "chainlink_issue_block",
            "chainlink_issue_relate",
            "chainlink_issue_cascade",
            "chainlink_milestone_create",
            "chainlink_milestone_list",
        ],
    );

    let worker_tools = list_tool_names(&runtime, "worker").await;
    assert_tools_present(
        "worker",
        &worker_tools,
        &[
            "chainlink_issue_show",
            "chainlink_session_start",
            "chainlink_issue_comment",
            "chainlink_session_work",
            "chainlink_session_end",
        ],
    );
    assert_tools_absent(
        "worker",
        &worker_tools,
        &[
            "chainlink_issue_create",
            "chainlink_session_status",
            "chainlink_subissue_create",
            "chainlink_subissue_close",
            "chainlink_issue_close",
            "chainlink_timer_start",
            "chainlink_timer_stop",
            "chainlink_timer_status",
            "chainlink_issue_list",
            "chainlink_issue_update",
            "chainlink_issue_block",
            "chainlink_issue_relate",
            "chainlink_issue_cascade",
            "chainlink_milestone_create",
            "chainlink_milestone_list",
        ],
    );
    assert_tools_absent("worker", &worker_tools, &coordinator_cleanup_tools);
    assert_tools_absent("worker", &worker_tools, &dropped_chainlink_tools);

    for role in ["reviewer", "testrunner"] {
        let names = list_tool_names(&runtime, role).await;
        assert_tools_absent(role, &names, &all_chainlink_tools);
        assert_tools_absent(role, &names, &coordinator_cleanup_tools);
        assert_tools_absent(role, &names, &dropped_chainlink_tools);
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_poll_workers_is_exposed_to_coordinators() {
    let runtime = build_test_runtime().await;

    for role in ["root", "tl"] {
        let tools: Vec<Value> = runtime
            .plugin_manager()
            .call("handle_list_tools", &json!({"role": role}))
            .await
            .unwrap_or_else(|error| panic!("handle_list_tools failed for {role}: {error}"));
        let names: Vec<&str> = tools
            .iter()
            .filter_map(|tool| tool["name"].as_str())
            .collect();
        assert!(
            names.contains(&"poll_workers"),
            "{role} role missing poll_workers. Got: {names:?}"
        );
    }

    for role in ["dev", "worker", "reviewer"] {
        let tools: Vec<Value> = runtime
            .plugin_manager()
            .call("handle_list_tools", &json!({"role": role}))
            .await
            .unwrap_or_else(|error| panic!("handle_list_tools failed for {role}: {error}"));
        let names: Vec<&str> = tools
            .iter()
            .filter_map(|tool| tool["name"].as_str())
            .collect();
        assert!(
            !names.contains(&"poll_workers"),
            "{role} role should not expose poll_workers"
        );
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_poll_workers_roundtrip_returns_worker_liveness_and_chainlink_state() {
    let runtime = build_test_runtime().await;

    let output = call_tool(
        &runtime,
        "tl",
        "poll_workers",
        json!({ "include_dead": true }),
    )
    .await;

    assert_tool_success(&output, "poll_workers");
    let worker = &output["result"]["workers"][0];
    assert_eq!(worker["name"], "worker-a");
    assert_eq!(worker["pane_id"], "%3");
    assert_eq!(worker["pane_alive"], true);
    assert_eq!(worker["age_mins"], 12);
    assert_eq!(worker["active_issue"], "7");
    assert_eq!(worker["issue_status"], "open");
    assert_eq!(
        worker["chainlink_session_state"],
        "active_in_current_session"
    );
    assert!(
        output["result"]["table"]
            .as_str()
            .is_some_and(|table| table.contains("worker-a")),
        "poll_workers table should include worker name: {output:#}"
    );
}

// ============================================================================
// Tool Roundtrip Tests (multi-effect trampoline)
// ============================================================================

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_spawn_leaf_roundtrip() {
    let runtime = build_test_runtime().await;

    let output = call_tool(
        &runtime,
        "tl",
        "spawn_leaf",
        json!({
            "name": "rust-handler",
            "task": "Implement the Rust handler",
        }),
    )
    .await;

    assert_tool_success(&output, "spawn_leaf");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_resume_pr_reports_verified_fresh_invocation_handoff() {
    let runtime = build_test_runtime().await;

    let output = call_tool(
        &runtime,
        "tl",
        "resume_pr",
        json!({
            "pr_number": 42,
            "task": "Apply the requested review fix",
        }),
    )
    .await;

    assert_tool_success(&output, "resume_pr");
    assert_eq!(output["result"]["invocation"]["trigger"], "resume_pr");
    assert_eq!(output["result"]["invocation"]["runtime"], "codex");
    assert_eq!(output["result"]["invocation"]["target_type"], "window");
    assert_eq!(output["result"]["invocation"]["target_id"], "@42");
    assert_eq!(output["result"]["invocation"]["fresh"], true);
    assert_eq!(output["result"]["invocation"]["ready"], true);
    assert_eq!(output["result"]["invocation"]["outcome"], "started");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_spawn_leaf_omits_agent_type_for_server_default() {
    let runtime = build_test_runtime().await;

    let output = call_tool(
        &runtime,
        "tl",
        "spawn_leaf",
        json!({
            "name": "rust-handler",
            "task": "Implement the Rust handler",
        }),
    )
    .await;

    assert_tool_success(&output, "spawn_leaf");
    assert_eq!(
        output["result"]["agent_type"], "unknown",
        "mock handler echoes the proto request; omitted agent_type must stay UNSPECIFIED so the real handler can use the configured worker default"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_spawn_leaf_passes_agent_type() {
    let runtime = build_test_runtime().await;

    let output = call_tool(
        &runtime,
        "tl",
        "spawn_leaf",
        json!({
            "name": "rust-handler",
            "task": "Implement the Rust handler",
            "agent_type": "opencode"
        }),
    )
    .await;

    assert_tool_success(&output, "spawn_leaf");
    assert_eq!(output["result"]["agent_type"], "opencode");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_spawn_leaf_preserves_explicit_model_separately_from_agent_type() {
    *mock_last_spawn_leaf_model().lock().unwrap() = None;
    let runtime = build_test_runtime().await;

    let output = call_tool(
        &runtime,
        "tl",
        "spawn_leaf",
        json!({
            "name": "model-routing",
            "task": "Exercise model propagation",
            "agent_type": "opencode",
            "model": "opencode-go/deepseek-v4-pro"
        }),
    )
    .await;

    assert_tool_success(&output, "spawn_leaf");
    assert_eq!(output["result"]["agent_type"], "opencode");
    assert_eq!(
        *mock_last_spawn_leaf_model().lock().unwrap(),
        Some("opencode-go/deepseek-v4-pro".to_owned())
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_spawn_leaf_passes_codex_agent_type() {
    let runtime = build_test_runtime().await;

    let output = call_tool(
        &runtime,
        "tl",
        "spawn_leaf",
        json!({
            "name": "rust-handler",
            "task": "Implement the Rust handler",
            "agent_type": "codex"
        }),
    )
    .await;

    assert_tool_success(&output, "spawn_leaf");
    assert_eq!(output["result"]["agent_type"], "codex");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_spawn_codex_roundtrip() {
    let runtime = build_test_runtime().await;

    let output = call_tool(
        &runtime,
        "tl",
        "spawn_codex",
        json!({
            "branch_name": "rust-codex",
            "task": "Implement the Rust handler"
        }),
    )
    .await;

    assert_tool_success(&output, "spawn_codex");
    assert_eq!(output["result"]["agent_type"], "codex");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_spawn_worker_roundtrip() {
    let runtime = build_test_runtime().await;

    let output = call_tool(
        &runtime,
        "tl",
        "spawn_worker",
        json!({
            "name": "rust-impl",
            "task": "Implement the Rust side"
        }),
    )
    .await;

    assert_tool_success(&output, "spawn_worker");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_spawn_worker_passes_agent_type() {
    let runtime = build_test_runtime().await;

    let output = call_tool(
        &runtime,
        "tl",
        "spawn_worker",
        json!({
            "name": "rust-impl",
            "task": "Implement the Rust side",
            "agent_type": "opencode"
        }),
    )
    .await;

    assert_tool_success(&output, "spawn_worker");
    assert_eq!(output["result"]["spawned"][0]["agent_type"], "opencode");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_spawn_worker_passes_codex_agent_type() {
    let runtime = build_test_runtime().await;

    let output = call_tool(
        &runtime,
        "tl",
        "spawn_worker",
        json!({
            "name": "rust-impl",
            "task": "Implement the Rust side",
            "agent_type": "codex"
        }),
    )
    .await;

    assert_tool_success(&output, "spawn_worker");
    assert_eq!(output["result"]["spawned"][0]["agent_type"], "codex");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_file_pr_roundtrip() {
    let runtime = build_test_runtime().await;

    let output = call_tool(
        &runtime,
        "tl",
        "file_pr",
        json!({
            "title": "Add feature X",
            "body": "Implements feature X as specified"
        }),
    )
    .await;

    assert_tool_success(&output, "file_pr");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_merge_pr_roundtrip() {
    let runtime = build_test_runtime().await;

    let output = call_tool(
        &runtime,
        "tl",
        "merge_pr",
        json!({
            "pr_number": 42
        }),
    )
    .await;

    assert_tool_success(&output, "merge_pr");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_merge_pr_rejects_stale_reviews_and_nonpassing_ci() {
    let runtime = build_test_runtime().await;
    let cases = [
        (43, "stale"),
        (44, "CI status"),
        (45, "requested changes"),
        (46, "CI status"),
        (47, "CI status"),
        (48, "commented"),
    ];

    for (pr_number, expected_error) in cases {
        let output = call_tool(&runtime, "tl", "merge_pr", json!({"pr_number": pr_number})).await;

        assert_tool_error(&output, "merge_pr gate");
        assert!(
            output["error"]
                .as_str()
                .is_some_and(|error| error.contains(expected_error)),
            "merge_pr error for PR #{pr_number} should mention {expected_error}: {output:#}"
        );
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_notify_parent_roundtrip() {
    let runtime = build_test_runtime().await;

    let output = call_tool(
        &runtime,
        "tl",
        "notify_parent",
        json!({
            "status": "success",
            "message": "All tasks completed"
        }),
    )
    .await;

    assert_tool_success(&output, "notify_parent");
}

// ============================================================================
// Argument Validation Tests
// ============================================================================

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_tool_unknown_name_returns_error() {
    let runtime = build_test_runtime().await;

    let output = call_tool(&runtime, "tl", "nonexistent_tool_xyz", json!({})).await;

    assert_tool_error(&output, "nonexistent tool");
}

// ============================================================================
// Error Propagation Tests
// ============================================================================

// ============================================================================
// Hook Tests
// ============================================================================

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_hook_session_start() {
    let runtime = build_test_runtime().await;

    let hook_input = json!({
        "role": "tl",
        "session_id": "test-session-123",
        "hook_event_name": "SessionStart"
    });

    let output: Value = runtime
        .plugin_manager()
        .call("handle_pre_tool_use", &hook_input)
        .await
        .expect("SessionStart hook failed");

    // SessionStart should return a valid hook response
    assert!(
        output.is_object(),
        "SessionStart should return an object: {output:#}"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_hook_worker_exit_notifies_parent_and_closes_self() {
    MOCK_AGENT_CLOSE_SELF_CALLS.store(0, Ordering::SeqCst);
    MOCK_EVENTS_NOTIFY_PARENT_CALLS.store(0, Ordering::SeqCst);

    let runtime = build_test_runtime().await;

    let hook_input = json!({
        "role": "worker",
        "session_id": "test-session",
        "hook_event_name": "WorkerExit",
        "agent_id": "test-worker-codex",
        "exit_status": "success"
    });

    let _output: Value = runtime
        .plugin_manager()
        .call("handle_pre_tool_use", &hook_input)
        .await
        .expect("WorkerExit hook failed");

    assert_eq!(
        MOCK_EVENTS_NOTIFY_PARENT_CALLS.load(Ordering::SeqCst),
        1,
        "WorkerExit should notify the parent once"
    );
    assert_eq!(
        MOCK_AGENT_CLOSE_SELF_CALLS.load(Ordering::SeqCst),
        1,
        "WorkerExit should reuse agent.close_self once"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_hook_pre_tool_use_allow() {
    let runtime = build_test_runtime().await;

    let hook_input = json!({
        "role": "tl",
        "session_id": "test-session",
        "hook_event_name": "PreToolUse",
        "tool_name": "Write"
    });

    let output: Value = runtime
        .plugin_manager()
        .call("handle_pre_tool_use", &hook_input)
        .await
        .expect("PreToolUse hook failed");

    // Default behavior should allow tool use
    assert!(
        output.is_object(),
        "PreToolUse should return an object: {output:#}"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_hook_pre_tool_use_blocks_gh_commands_for_dev() {
    let runtime = build_test_runtime().await;

    let hook_input = json!({
        "role": "dev",
        "session_id": "test-session",
        "hook_event_name": "PreToolUse",
        "tool_name": "bash",
        "tool_input": {"command": "gh auth status"}
    });

    let output: Value = runtime
        .plugin_manager()
        .call("handle_pre_tool_use", &hook_input)
        .await
        .expect("PreToolUse hook failed");

    assert_eq!(
        output["continue"], false,
        "gh command should be blocked: {output:#}"
    );
    assert!(
        output["stopReason"]
            .as_str()
            .unwrap_or_default()
            .contains("Do not run gh commands"),
        "gh command denial should explain the policy: {output:#}"
    );
    assert_eq!(
        output["hookSpecificOutput"]["permissionDecision"], "deny",
        "gh command should return a deny permission decision: {output:#}"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_hook_pre_tool_use_blocks_chainlink_sqlite_for_all_agent_roles() {
    let runtime = build_test_runtime().await;

    for role in ["root", "tl", "dev", "worker", "reviewer"] {
        let hook_input = json!({
            "role": role,
            "session_id": "test-session",
            "hook_event_name": "PreToolUse",
            "tool_name": "bash",
            "tool_input": {
                "command": "sqlite3 .chainlink/issues.db 'select * from issues'"
            }
        });

        let output: Value = runtime
            .plugin_manager()
            .call("handle_pre_tool_use", &hook_input)
            .await
            .unwrap_or_else(|e| panic!("PreToolUse hook failed for {role}: {e}"));

        assert_eq!(
            output["continue"], false,
            "{role} should block direct Chainlink sqlite access: {output:#}"
        );
        assert!(
            output["stopReason"]
                .as_str()
                .unwrap_or_default()
                .contains("Do not access Chainlink sqlite databases directly"),
            "{role} sqlite denial should explain the policy: {output:#}"
        );
        assert_eq!(
            output["hookSpecificOutput"]["permissionDecision"], "deny",
            "{role} sqlite command should return a deny permission decision: {output:#}"
        );
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_hook_pre_tool_use_allows_chainlink_cli_commands() {
    let runtime = build_test_runtime().await;

    let hook_input = json!({
        "role": "tl",
        "session_id": "test-session",
        "hook_event_name": "PreToolUse",
        "tool_name": "bash",
        "tool_input": {"command": "chainlink issue list --json"}
    });

    let output: Value = runtime
        .plugin_manager()
        .call("handle_pre_tool_use", &hook_input)
        .await
        .expect("PreToolUse hook failed");

    assert_eq!(
        output["continue"], true,
        "chainlink CLI command should be allowed: {output:#}"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_hook_pre_tool_use_allows_words_containing_gh() {
    let runtime = build_test_runtime().await;

    let hook_input = json!({
        "role": "dev",
        "session_id": "test-session",
        "hook_event_name": "PreToolUse",
        "tool_name": "bash",
        "tool_input": {"command": "printf '%s\\n' ghost"}
    });

    let output: Value = runtime
        .plugin_manager()
        .call("handle_pre_tool_use", &hook_input)
        .await
        .expect("PreToolUse hook failed");

    assert_eq!(
        output["continue"], true,
        "non-gh command should be allowed: {output:#}"
    );
}

/// Diagnostic: spawn_leaf with timeout to observe trampoline logs.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_spawn_leaf_timeout_diagnostic() {
    let runtime = build_test_runtime().await;

    eprintln!("=== DIAGNOSTIC: Starting spawn_leaf call ===");

    let result = tokio::time::timeout(
        std::time::Duration::from_secs(30),
        call_tool(
            &runtime,
            "tl",
            "spawn_leaf",
            json!({
                "name": "test-leaf",
                "task": "Test task",
            }),
        ),
    )
    .await;

    match result {
        Ok(output) => {
            eprintln!("=== DIAGNOSTIC: spawn_leaf completed: {output:#} ===");
            assert_tool_success(&output, "spawn_leaf (diagnostic)");
        }
        Err(_) => {
            eprintln!("=== DIAGNOSTIC: spawn_leaf TIMED OUT after 30s ===");
            panic!("spawn_leaf hung — check trampoline logs above");
        }
    }
}

/// Diagnostic: spawn_worker with timeout.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn wasm_spawn_worker_timeout_diagnostic() {
    let runtime = build_test_runtime().await;

    eprintln!("=== DIAGNOSTIC: Starting spawn_worker call ===");

    let result = tokio::time::timeout(
        std::time::Duration::from_secs(30),
        call_tool(
            &runtime,
            "tl",
            "spawn_worker",
            json!({
                "name": "diag-worker",
                "task": "Test task"
            }),
        ),
    )
    .await;

    match result {
        Ok(output) => {
            eprintln!("=== DIAGNOSTIC: spawn_worker completed: {output:#} ===");
            assert_tool_success(&output, "spawn_worker (diagnostic)");
        }
        Err(_) => {
            eprintln!("=== DIAGNOSTIC: spawn_worker TIMED OUT after 30s ===");
            panic!("spawn_worker hung — check trampoline logs above");
        }
    }
}
