use super::{
    assert_tool_error, build_production_agent_handler, build_production_agent_runtime,
    build_wasm_runtime_with_agent_handler, call_tool, mount_open_pr, run_git, AgentHandler,
};
use async_trait::async_trait;
use exomonad_core::services::event_log::EventLog;
use exomonad_core::services::forgejo::ForgejoClient;
use exomonad_core::services::immutable_ledger::LedgerWriter;
use exomonad_core::{EffectContext, EffectError, EffectHandler, EffectResult};
use exomonad_proto::effects::agent as agent_proto;
use prost::Message;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::process::Command;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const DIRTY_WORKTREE_CODE: &str = "agent.spawn_precondition.dirty_worktree";

struct OwnerFixture {
    name: String,
    branch: String,
    slice_id: String,
    worktree_relative: PathBuf,
    invocation_path: PathBuf,
    invocation_bytes: Vec<u8>,
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial_test::serial]
async fn compiled_resume_blocked_leaf_preserves_typed_dirty_refusal_without_intent() {
    let temp = tempfile::tempdir().expect("temporary blocked-resume fixture");
    let project = temp.path().join("checkout");
    let remote = temp.path().join("fixture.git");
    let owner_name = "blocked-owner-codex";
    let owner_branch = format!("main.{owner_name}");
    let worktree = initialize_resume_repository(&project, &remote, owner_name, &owner_branch);
    let issue_id = create_open_chainlink_issue(&project);
    let owner = seed_dormant_owner(
        &project,
        owner_name,
        &owner_branch,
        &worktree,
        "slice-blocked-resume",
        Some(issue_id),
    );
    append_parked_event(&project, &owner.slice_id);
    std::fs::write(
        project.join("SOURCE-DIRTY.txt"),
        "uncommitted source edit\n",
    )
    .expect("dirty source worktree for preflight");
    let fingerprint = worktree_fingerprint(&project.join(&owner.worktree_relative));
    let forgejo = MockServer::start().await;
    let client = ForgejoClient::new(&forgejo.uri(), "test-only-token")
        .expect("construct isolated Forgejo client");
    let runtime = build_production_agent_runtime(&project, client).await;

    let output = call_tool(
        &runtime,
        "tl",
        "resume_blocked_leaf",
        json!({
            "chainlink_issue_id": issue_id,
            "expected_invocation_id": "parked-invocation-1",
            "expected_branch": owner.branch,
            "expected_worktree_fingerprint": fingerprint,
            "task": "Continue the existing parked assignment",
            "human_approved": true
        }),
    )
    .await;

    assert_typed_dirty_refusal(&output, "resume_blocked_leaf");
    let events = read_project_events(&project);
    let failed = only_spawn_failed(&events);
    assert_eq!(failed["data"]["child_agent"], owner.name);
    assert!(failed["data"].get("intent_id").is_none());
    assert_eq!(failed["data"]["code"], DIRTY_WORKTREE_CODE);
    assert_eq!(
        std::fs::read(&owner.invocation_path).expect("read parked invocation after refusal"),
        owner.invocation_bytes,
        "preflight refusal must not create or replace the parked invocation"
    );
    assert_eq!(agent_directory_names(&project), [owner.name.as_str()]);
    assert_no_spawn_or_resume_event(&events);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial_test::serial]
async fn compiled_resume_pr_preserves_typed_dirty_refusal_after_owner_and_head_checks() {
    let temp = tempfile::tempdir().expect("temporary PR-resume fixture");
    let project = temp.path().join("checkout");
    let remote = temp.path().join("fixture.git");
    let owner_name = "pr-owner-codex";
    let owner_branch = format!("main.{owner_name}");
    let (worktree, head_sha) =
        initialize_resume_repository_with_pr(&project, &remote, owner_name, &owner_branch);
    let owner = seed_dormant_owner(
        &project,
        owner_name,
        &owner_branch,
        &worktree,
        "slice-pr-resume",
        None,
    );
    std::fs::write(
        project.join("SOURCE-DIRTY.txt"),
        "uncommitted source edit\n",
    )
    .expect("dirty source worktree for preflight");
    let forgejo = MockServer::start().await;
    mount_resume_pr_observations(&forgejo, 46, &owner.branch, &head_sha).await;
    let client = ForgejoClient::new(&forgejo.uri(), "test-only-token")
        .expect("construct isolated Forgejo client");
    let production = build_production_agent_handler(&project, client).await;
    let runtime = build_wasm_runtime_with_agent_handler(
        &project,
        ResumePrWatcher {
            production,
            pr_number: 46,
            branch: owner.branch.clone(),
            head_sha: head_sha.clone(),
        },
    )
    .await;

    let stale = call_tool(
        &runtime,
        "tl",
        "resume_pr",
        json!({
            "pr_number": 46,
            "expected_head_sha": "stale-head-sha",
            "task": "Resume the existing owner after review"
        }),
    )
    .await;
    assert_tool_error(&stale, "resume_pr");
    assert_eq!(stale["error_kind"], Value::Null);
    assert!(
        stale["error"]
            .as_str()
            .unwrap_or_default()
            .contains("head changed"),
        "stale PR head should be refused before host resume: {stale:#}"
    );
    assert!(read_project_events(&project).is_empty());

    let output = call_tool(
        &runtime,
        "tl",
        "resume_pr",
        json!({
            "pr_number": 46,
            "expected_head_sha": head_sha,
            "task": "Resume the existing owner after review"
        }),
    )
    .await;

    assert_typed_dirty_refusal(&output, "resume_pr");
    let events = read_project_events(&project);
    let failed = only_spawn_failed(&events);
    assert_eq!(failed["data"]["child_agent"], owner.name);
    assert_eq!(failed["data"]["code"], DIRTY_WORKTREE_CODE);
    assert!(failed["data"].get("intent_id").is_none());
    assert_eq!(
        std::fs::read(&owner.invocation_path).expect("read prior PR invocation after refusal"),
        owner.invocation_bytes,
        "typed refusal must preserve the existing owner invocation"
    );
    assert_eq!(agent_directory_names(&project), [owner.name.as_str()]);
    assert_no_spawn_or_resume_event(&events);
}

fn initialize_resume_repository(
    project: &Path,
    remote: &Path,
    owner_name: &str,
    owner_branch: &str,
) -> PathBuf {
    initialize_repository(project, remote);
    create_owner_worktree(project, owner_name, owner_branch)
}

fn initialize_resume_repository_with_pr(
    project: &Path,
    remote: &Path,
    owner_name: &str,
    owner_branch: &str,
) -> (PathBuf, String) {
    let worktree = initialize_resume_repository(project, remote, owner_name, owner_branch);
    std::fs::write(worktree.join("review-fix.txt"), "published owner change\n")
        .expect("write PR commit");
    run_git(&worktree, &["add", "review-fix.txt"]);
    run_git(&worktree, &["commit", "--quiet", "-m", "owner PR head"]);
    let head_sha = git_output(&worktree, &["rev-parse", "HEAD"]);
    run_git(project, &["push", "fixture", owner_branch]);
    (worktree, head_sha)
}

fn initialize_repository(project: &Path, remote: &Path) {
    std::fs::create_dir_all(project).expect("create fixture repository");
    run_git(project, &["init", "--quiet", "--initial-branch=main"]);
    run_git(project, &["config", "user.name", "WASM resume fixture"]);
    run_git(
        project,
        &["config", "user.email", "resume-fixture@example.invalid"],
    );
    std::fs::write(project.join(".gitignore"), ".exo/\n.chainlink/\n")
        .expect("write fixture ignores");
    std::fs::write(project.join("README.md"), "fixture baseline\n")
        .expect("write fixture baseline");
    run_git(project, &["add", ".gitignore", "README.md"]);
    run_git(project, &["commit", "--quiet", "-m", "fixture baseline"]);
    let remote_path = remote.to_str().expect("fixture remote path is UTF-8");
    run_git(project, &["init", "--bare", "--quiet", remote_path]);
    run_git(
        project,
        &[
            "remote",
            "add",
            "origin",
            "https://forgejo.local/acme/repo.git",
        ],
    );
    run_git(project, &["remote", "add", "fixture", remote_path]);
    run_git(project, &["push", "fixture", "main"]);
}

struct ResumePrWatcher {
    production: AgentHandler<super::Services>,
    pr_number: u64,
    branch: String,
    head_sha: String,
}

#[async_trait]
impl EffectHandler for ResumePrWatcher {
    fn namespace(&self) -> &str {
        "agent"
    }

    async fn handle(
        &self,
        effect_type: &str,
        payload: &[u8],
        ctx: &EffectContext,
    ) -> EffectResult<Vec<u8>> {
        if effect_type != "agent.watcher_pr_state" {
            return self.production.handle(effect_type, payload, ctx).await;
        }
        let request = agent_proto::WatcherPrStateRequest::decode(payload)
            .map_err(|error| EffectError::invalid_input(error.to_string()))?;
        if request.pr_number != self.pr_number {
            return Err(EffectError::invalid_input(format!(
                "unexpected watcher PR {}",
                request.pr_number
            )));
        }
        Ok(agent_proto::WatcherPrStateResponse {
            success: true,
            pr_number: self.pr_number,
            found: true,
            review_state: "pending".into(),
            ci_status: "success".into(),
            head_sha: self.head_sha.clone(),
            head_branch: self.branch.clone(),
            base_branch: "main".into(),
            pr_state: "open".into(),
            head_reachable: true,
            ..Default::default()
        }
        .encode_to_vec())
    }
}

fn create_owner_worktree(project: &Path, owner_name: &str, owner_branch: &str) -> PathBuf {
    let relative = PathBuf::from(".exo/worktrees").join(owner_name);
    let full = project.join(&relative);
    let full_path = full.to_str().expect("owner worktree path is UTF-8");
    run_git(
        project,
        &["worktree", "add", "--quiet", "-b", owner_branch, full_path],
    );
    full
}

fn seed_dormant_owner(
    project: &Path,
    owner_name: &str,
    owner_branch: &str,
    worktree: &Path,
    slice_id: &str,
    active_issue: Option<u64>,
) -> OwnerFixture {
    let relative_worktree = worktree
        .strip_prefix(project)
        .expect("owner worktree is inside project")
        .to_path_buf();
    let agent_dir = project.join(".exo/agents").join(owner_name);
    std::fs::create_dir_all(&agent_dir).expect("create durable owner checkpoint");
    std::fs::write(
        agent_dir.join("identity.json"),
        serde_json::to_vec(&json!({
            "agent_name": owner_name,
            "slug": owner_name.strip_suffix("-codex").unwrap_or(owner_name),
            "agent_type": "codex",
            "birth_branch": owner_branch,
            "parent_branch": "main",
            "working_dir": relative_worktree,
            "display_name": format!("🤖 {owner_name}"),
            "topology": "worktree_per_agent",
            "ledger_owned": true,
            "slice_id": slice_id
        }))
        .expect("serialize durable owner identity"),
    )
    .expect("write durable owner identity");
    if let Some(issue_id) = active_issue {
        std::fs::write(agent_dir.join("active_issue"), issue_id.to_string())
            .expect("write active issue binding");
    }
    let invocation_path = agent_dir.join("invocation.json");
    let invocation_bytes = serde_json::to_vec(&json!({
        "invocation_id": "parked-invocation-1",
        "runtime": "codex",
        "trigger": "spawn",
        "mode": "one_shot",
        "routing": { "window_id": null, "pane_id": null, "parent_tab": null },
        "started_at": 1,
        "ended_at": 2,
        "status": "exited",
        "exit_code": 1,
        "generation": 1,
        "slice_id": slice_id,
        "branch": owner_branch,
        "worktree": relative_worktree
    }))
    .expect("serialize prior invocation");
    std::fs::write(&invocation_path, &invocation_bytes).expect("write prior invocation");
    OwnerFixture {
        name: owner_name.to_string(),
        branch: owner_branch.to_string(),
        slice_id: slice_id.to_string(),
        worktree_relative: relative_worktree,
        invocation_path,
        invocation_bytes,
    }
}

fn create_open_chainlink_issue(project: &Path) -> u64 {
    let init = Command::new("chainlink")
        .args(["init", "--no-hooks", "--quiet"])
        .current_dir(project)
        .output()
        .expect("Chainlink CLI is available to create isolated issue fixture");
    assert!(
        init.status.success(),
        "initialize temporary Chainlink database: {}",
        String::from_utf8_lossy(&init.stderr)
    );
    let created = Command::new("chainlink")
        .args([
            "issue",
            "create",
            "Disposable blocked resume fixture",
            "--priority",
            "low",
            "--quiet",
        ])
        .current_dir(project)
        .output()
        .expect("create open temporary Chainlink issue");
    assert!(
        created.status.success(),
        "create temporary Chainlink issue: {}",
        String::from_utf8_lossy(&created.stderr)
    );
    String::from_utf8_lossy(&created.stdout)
        .trim()
        .parse()
        .expect("quiet issue creation returns its numeric ID")
}

fn append_parked_event(project: &Path, slice_id: &str) {
    EventLog::open(project.join(".exo/logs"))
        .expect("open temporary event log")
        .append(
            "tl.slice_parked",
            "root",
            &json!({ "slice_id": slice_id, "park_cause": "external_dependency" }),
        )
        .expect("record authoritative parked event");
}

fn worktree_fingerprint(worktree: &Path) -> String {
    let output = Command::new("git")
        .args(["status", "--porcelain=v1", "-z"])
        .current_dir(worktree)
        .output()
        .expect("inspect parked owner worktree");
    assert!(output.status.success(), "parked worktree status succeeds");
    format!("sha256:{:x}", Sha256::digest(output.stdout))
}

async fn mount_resume_pr_observations(
    server: &MockServer,
    number: u64,
    branch: &str,
    head_sha: &str,
) {
    mount_open_pr(server, number, branch, head_sha).await;
    Mock::given(method("GET"))
        .and(path(format!(
            "/api/v1/repos/acme/repo/pulls/{number}/reviews"
        )))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!([])))
        .mount(server)
        .await;
    Mock::given(method("GET"))
        .and(path(format!(
            "/api/v1/repos/acme/repo/commits/{head_sha}/status"
        )))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "state": "pending",
            "statuses": []
        })))
        .mount(server)
        .await;
}

fn git_output(project: &Path, arguments: &[&str]) -> String {
    let output = Command::new("git")
        .args(arguments)
        .current_dir(project)
        .output()
        .expect("run Git fixture command");
    assert!(
        output.status.success(),
        "Git command {arguments:?} failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

fn assert_typed_dirty_refusal(output: &Value, tool: &str) {
    assert_tool_error(output, tool);
    assert_eq!(
        output["error_kind"], DIRTY_WORKTREE_CODE,
        "compiled {tool} response must retain the production preflight classification: {output:#}"
    );
    assert!(
        output["error"]
            .as_str()
            .is_some_and(|message| message.starts_with("TL preflight failed; spawning is blocked")),
        "the production diagnostic must remain visible: {output:#}"
    );
}

fn read_project_events(project: &Path) -> Vec<Value> {
    LedgerWriter::open_project(project)
        .expect("open project event ledger")
        .read_resolved_events()
        .expect("read project events")
        .into_iter()
        .map(|record| {
            json!({
                "event_type": record.event.event_type,
                "agent_id": record.event.agent_id,
                "data": record.event.data
            })
        })
        .collect()
}

fn only_spawn_failed(events: &[Value]) -> &Value {
    let failures = events
        .iter()
        .filter(|event| event["event_type"] == "agent.spawn_failed")
        .collect::<Vec<_>>();
    assert_eq!(failures.len(), 1, "exactly one failure event is recorded");
    failures[0]
}

fn agent_directory_names(project: &Path) -> Vec<String> {
    let mut names = std::fs::read_dir(project.join(".exo/agents"))
        .expect("read durable owner directories")
        .map(|entry| {
            entry
                .expect("read owner entry")
                .file_name()
                .to_string_lossy()
                .into_owned()
        })
        .collect::<Vec<_>>();
    names.sort();
    names
}

fn assert_no_spawn_or_resume_event(events: &[Value]) {
    assert!(!events.iter().any(|event| {
        matches!(
            event["event_type"].as_str(),
            Some("agent.spawned" | "agent.resumed")
        )
    }));
}
