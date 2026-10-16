use super::*;
use exomonad_core::domain::{AgentName, BirthBranch, Slug};
use exomonad_core::handlers::ProcessHandler;
use exomonad_core::services::agent_control::{AgentType, Topology};
use exomonad_core::services::agent_resolver::{AgentIdentityRecord, AgentResolver};
use exomonad_core::services::forgejo::ForgejoClient;
use exomonad_core::services::pr_registry::{
    publish_verified_head, InvocationSuccession, PublicationProvenance, PublishedHead,
};
use exomonad_core::services::Services;
use exomonad_core::EffectHandler;
use exomonad_proto::effects::{agent, github};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use tempfile::TempDir;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const REPOSITORY_URL: &str = "ssh://forgejo.test/acme/repo.git";
const PR_NUMBER: u64 = 46;
const PARENT_RUN: &str = "root-run";
const CHILD_SCOPE: &str = "architecture";
const CHILD_MANIFEST_ID: &str = "root-run/sub_tl/architecture";
const PARENT_BRANCH: &str = "main";
const HEAD_BRANCH: &str = "main.architecture";
const CONTROLLER_EPOCH: &str = "epoch-current";
const PUBLISHER: &str = "architecture";
const PUBLISHER_INVOCATION: &str = "architecture-invocation";

#[derive(Clone, Copy)]
enum Outcome {
    Merge,
    CommitThenUnknownResponse,
}

struct ForgejoState {
    head_sha: String,
    forgejo_head_sha: Mutex<String>,
    base_branch: Mutex<String>,
    ci_status: Mutex<String>,
    merged: AtomicBool,
    merge_calls: AtomicUsize,
    bare_repository: PathBuf,
    merge_head: String,
    outcome: Outcome,
}

struct Fixture {
    _directory: TempDir,
    project_dir: PathBuf,
    bare_repository: PathBuf,
    working_dir: String,
    base_sha: String,
    head_sha: String,
    head_tree_sha: String,
    patch_digest: String,
    merge_tree_sha: String,
    receipt_id: String,
    reference: Value,
    superseded_reference: Value,
}

impl Fixture {
    async fn new() -> Self {
        let current_dir = std::env::current_dir().expect("test working directory");
        let directory = tempfile::tempdir_in(&current_dir).expect("temporary fixture root");
        let project_dir = directory.path().join("project");
        let bare_repository = directory.path().join("remote.git");
        std::fs::create_dir_all(&project_dir).expect("create repository worktree");
        Self::initialize_git(&project_dir, &bare_repository);

        let base_sha = git_output(&project_dir, &["rev-parse", "main"]);
        let head_sha = git_output(&project_dir, &["rev-parse", HEAD_BRANCH]);
        let aggregate_worktree = project_dir.join(".exo/worktrees/architecture");
        let head_tree_sha = git_output(
            &aggregate_worktree,
            &["rev-parse", &format!("{head_sha}^{{tree}}")],
        );
        let diff_range = format!("{base_sha}...{head_sha}");
        let patch = git_bytes(
            &project_dir,
            &["diff", "--binary", "--no-ext-diff", &diff_range],
        );
        let patch_digest = format!("{:x}", Sha256::digest(patch));
        let merge_tree_output = git_output(
            &project_dir,
            &["merge-tree", "--write-tree", &base_sha, &head_sha],
        );
        let merge_tree_sha = merge_tree_output
            .split_whitespace()
            .next()
            .expect("merge tree object ID")
            .to_string();
        let working_dir = project_dir
            .strip_prefix(current_dir)
            .expect("fixture lives below Cargo's test working directory")
            .to_str()
            .expect("UTF-8 relative fixture path")
            .to_string();

        let mut fixture = Self {
            _directory: directory,
            project_dir,
            bare_repository,
            working_dir,
            base_sha,
            head_sha,
            head_tree_sha,
            patch_digest,
            merge_tree_sha,
            receipt_id: String::new(),
            reference: Value::Null,
            superseded_reference: Value::Null,
        };
        fixture.write_authoritative_state();
        fixture.write_publication_history().await;
        fixture
    }

    fn initialize_git(project_dir: &Path, bare_repository: &Path) {
        git(project_dir, &["init", "--quiet"]);
        git(project_dir, &["config", "user.name", "Integration Test"]);
        git(
            project_dir,
            &["config", "user.email", "integration@example.test"],
        );
        git(project_dir, &["branch", "-M", PARENT_BRANCH]);
        std::fs::write(project_dir.join("base.txt"), "base\n").expect("write base file");
        git(project_dir, &["add", "base.txt"]);
        git(project_dir, &["commit", "--quiet", "-m", "base"]);

        std::fs::create_dir_all(bare_repository).expect("create bare remote directory");
        git(bare_repository, &["init", "--bare", "--quiet"]);
        let ssh_script = bare_repository.with_extension("ssh-fixture.sh");
        let ssh_contents = format!(
            "#!/bin/sh\ncase \"$*\" in\n  *git-upload-pack*) exec git-upload-pack '{}' ;;\n  *git-receive-pack*) exec git-receive-pack '{}' ;;\n  *) exit 2 ;;\nesac\n",
            bare_repository.display(),
            bare_repository.display(),
        );
        std::fs::write(&ssh_script, ssh_contents).expect("write fixture SSH transport");
        let ssh_command = format!("sh {}", ssh_script.display());
        git(
            project_dir,
            &["config", "--local", "core.sshCommand", &ssh_command],
        );
        git(project_dir, &["remote", "add", "origin", REPOSITORY_URL]);
        git(
            project_dir,
            &["push", "--quiet", "--set-upstream", "origin", PARENT_BRANCH],
        );
        git(project_dir, &["switch", "--quiet", "-c", HEAD_BRANCH]);
        std::fs::write(
            project_dir.join("architecture.txt"),
            "reviewed architecture\n",
        )
        .expect("write reviewed child change");
        git(project_dir, &["add", "architecture.txt"]);
        git(
            project_dir,
            &["commit", "--quiet", "-m", "reviewed architecture child"],
        );
        git(
            project_dir,
            &["push", "--quiet", "--set-upstream", "origin", HEAD_BRANCH],
        );
        git(project_dir, &["switch", "--quiet", PARENT_BRANCH]);
        std::fs::create_dir_all(project_dir.join(".exo/worktrees"))
            .expect("create aggregate worktree parent");
        git(
            project_dir,
            &[
                "worktree",
                "add",
                "--quiet",
                ".exo/worktrees/architecture",
                HEAD_BRANCH,
            ],
        );
    }

    fn write_authoritative_state(&mut self) {
        let repository_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let generator =
            repository_root.join("tl_loop/tests/fixtures/aggregate_approval_producer.py");
        let python_venv = repository_root.join("tl_loop/.venv/bin/python");
        let python = if python_venv.exists() {
            python_venv
        } else {
            PathBuf::from("python3")
        };
        let scope_root = self.project_dir.join(".exo/tl-loop");
        let lineage_proof = json!({
            "aggregate_head_sha": self.head_sha,
            "aggregate_tree_sha": self.head_tree_sha,
            "relations": [{
                "aggregate_commit": self.head_sha,
                "receipt_commit": self.head_sha,
                "relation": "ancestor",
            }],
        });
        let lineage_proof_digest = digest(&lineage_proof);
        let request = json!({
            "parent_scope_path": [PARENT_RUN],
            "ancestor_child_branches": [],
            "scope_root": scope_root,
            "root_branch": PARENT_BRANCH,
            "parent_run_id": PARENT_RUN,
            "parent_agent_id": "root",
            "parent_branch": PARENT_BRANCH,
            "child_run_id": CHILD_SCOPE,
            "child_branch": HEAD_BRANCH,
            "child_task": "Integrate reviewed architecture work",
            "repository_url": REPOSITORY_URL,
            "controller_epoch": CONTROLLER_EPOCH,
            "pr_number": PR_NUMBER,
            "head_sha": self.head_sha,
            "head_tree_sha": self.head_tree_sha,
            "base_sha": self.base_sha,
            "patch_digest": self.patch_digest,
            "merge_tree_sha": self.merge_tree_sha,
            "ci_status": "success",
            "readiness_receipt_digest": "2222222222222222222222222222222222222222222222222222222222222222",
            "lineage_proof_digest": lineage_proof_digest,
            "lineage_commit_shas": [self.head_sha],
            "worktree": ".exo/worktrees/architecture",
        });
        let mut process = Command::new(python)
            .arg(generator)
            .env("PYTHONPATH", &repository_root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("start production Python approval producer");
        process
            .stdin
            .take()
            .expect("producer stdin")
            .write_all(&serde_json::to_vec(&request).expect("producer request JSON"))
            .expect("send producer request");
        let output = process
            .wait_with_output()
            .expect("wait for Python producer");
        assert!(
            output.status.success(),
            "production Python approval producer failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        let artifact: Value = serde_json::from_slice(&output.stdout).expect("Python artifact JSON");
        self.receipt_id = artifact["receipt_id"]
            .as_str()
            .expect("receipt ID")
            .to_string();
        self.reference = artifact["reference"].clone();
        self.superseded_reference = artifact["superseded_reference"].clone();
        assert_eq!(artifact["parent_manifest_scope_id"], PARENT_RUN);
        assert_eq!(artifact["child_manifest_node_id"], CHILD_MANIFEST_ID);
        assert_eq!(artifact["child_manifest_scope_id"], CHILD_MANIFEST_ID);
        for checkpoint in artifact["checkpoints"].as_array().expect("checkpoint list") {
            let mut scope = scope_root.clone();
            for segment in checkpoint["scope_path"].as_array().expect("scope path") {
                scope.push(segment.as_str().expect("scope path segment"));
            }
            std::fs::create_dir_all(&scope).expect("create generated scope checkpoint directory");
            std::fs::write(
                scope.join("run.json"),
                serde_json::to_vec(&checkpoint["state"]).expect("serialize generated RunStore"),
            )
            .expect("write Python-generated RunStore");
        }
        self.write_resolver_identities();
    }

    fn write_resolver_identities(&self) {
        let resolver_dir = self.project_dir.join(".exo/agents/architecture");
        std::fs::create_dir_all(&resolver_dir).expect("create publisher resolver directory");
        std::fs::write(
            resolver_dir.join("invocation.json"),
            json!({
                "invocation_id": PUBLISHER_INVOCATION,
                "runtime": "codex",
                "trigger": "spawn",
                "mode": "interactive",
                "routing": {
                    "window_id": null,
                    "pane_id": null,
                    "parent_tab": null,
                    "owner_agent_id": null,
                    "owner_invocation_id": null,
                    "owner_generation": null,
                },
                "started_at": 1,
                "ended_at": null,
                "status": "running",
                "exit_code": null,
                "pr_number": PR_NUMBER,
                "head_sha": self.head_sha,
                "generation": 1,
                "runtime_agent_id": PUBLISHER,
                "slice_id": CHILD_SCOPE,
                "branch": HEAD_BRANCH,
                "worktree": ".exo/worktrees/architecture",
            })
            .to_string(),
        )
        .expect("write publisher invocation record");
    }

    fn merge_arguments(&self, reference: Value) -> Value {
        json!({
            "pr_number": PR_NUMBER,
            "strategy": "merge",
            "working_dir": self.working_dir,
            "expected_base_sha": self.base_sha,
            "expected_head_sha": self.head_sha,
            "expected_patch_digest": self.patch_digest,
            "expected_merge_tree_sha": self.merge_tree_sha,
            "aggregate_approval": reference,
        })
    }

    async fn write_publication_history(&self) {
        let published = PublishedHead {
            pr_number: PR_NUMBER,
            head_branch: HEAD_BRANCH.to_string(),
            base_branch: PARENT_BRANCH.to_string(),
            head_sha: self.head_sha.clone(),
            author_agent: Some(PUBLISHER.to_string()),
            author_role: Some("tl".to_string()),
            provenance: PublicationProvenance::LedgerOwned,
            slice_id: Some(CHILD_SCOPE.to_string()),
            invocation_id: Some(PUBLISHER_INVOCATION.to_string()),
            invocation_trigger: Some("spawn".to_string()),
            invocation_runtime: Some("codex".to_string()),
            invocation_succession: Vec::<InvocationSuccession>::new(),
        };
        publish_verified_head(&self.project_dir, published)
            .await
            .expect("write aggregate publication history");
    }
}

struct ParentGithubHandler {
    head_sha: String,
}

#[async_trait]
impl EffectHandler for ParentGithubHandler {
    fn namespace(&self) -> &str {
        "github"
    }

    async fn handle(
        &self,
        effect_type: &str,
        payload: &[u8],
        _ctx: &exomonad_core::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        if effect_type != "github.get_pull_request" {
            return Err(EffectError::not_found(format!(
                "aggregate_github/{effect_type}"
            )));
        }
        let request = github::GetPullRequestRequest::decode(payload)
            .map_err(|error| EffectError::invalid_input(error.to_string()))?;
        Ok(github::GetPullRequestResponse {
            pull_request: Some(github::PullRequest {
                number: request.number,
                state: github::IssueState::Open as i32,
                head_ref: HEAD_BRANCH.to_string(),
                base_ref: PARENT_BRANCH.to_string(),
                head_sha: self.head_sha.clone(),
                ..Default::default()
            }),
            reviews: Vec::new(),
        }
        .encode_to_vec())
    }
}

struct ParentWatcherHandler {
    state: Arc<ForgejoState>,
    base_sha: String,
    head_tree_sha: String,
    patch_digest: String,
    merge_tree_sha: String,
}

#[async_trait]
impl EffectHandler for ParentWatcherHandler {
    fn namespace(&self) -> &str {
        "agent"
    }

    async fn handle(
        &self,
        effect_type: &str,
        payload: &[u8],
        _ctx: &exomonad_core::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        if effect_type != "agent.watcher_pr_state" {
            return Err(EffectError::not_found(format!(
                "aggregate_agent/{effect_type}"
            )));
        }
        let request = agent::WatcherPrStateRequest::decode(payload)
            .map_err(|error| EffectError::invalid_input(error.to_string()))?;
        Ok(agent::WatcherPrStateResponse {
            success: true,
            pr_number: request.pr_number,
            found: true,
            review_state: "pending".to_string(),
            ci_status: "success".to_string(),
            head_sha: self.state.head_sha.clone(),
            head_branch: HEAD_BRANCH.to_string(),
            base_branch: PARENT_BRANCH.to_string(),
            base_sha: self.base_sha.clone(),
            patch_digest: self.patch_digest.clone(),
            merge_tree_sha: self.merge_tree_sha.clone(),
            pr_head_tree_sha: self.head_tree_sha.clone(),
            pr_state: if self.state.merged.load(Ordering::SeqCst) {
                "closed".to_string()
            } else {
                "open".to_string()
            },
            merged: self.state.merged.load(Ordering::SeqCst),
            head_reachable: true,
            publication_ownership_verified: true,
            ..Default::default()
        }
        .encode_to_vec())
    }
}

async fn build_production_merge_runtime(
    fixture: &Fixture,
    forgejo: Arc<ForgejoClient>,
    state: Arc<ForgejoState>,
) -> exomonad_core::Runtime {
    let mut services = Services::benchmark(fixture.project_dir.clone())
        .expect("construct isolated production MergePR services");
    services.forgejo_client = Some(forgejo);
    let resolver = AgentResolver::load(fixture.project_dir.clone()).await;
    for identity in [
        owner_identity("root", "root", "main", "main", None, PathBuf::from(".")),
        owner_identity(
            PUBLISHER,
            CHILD_SCOPE,
            HEAD_BRANCH,
            PARENT_BRANCH,
            Some(CHILD_SCOPE),
            PathBuf::from(".exo/worktrees/architecture"),
        ),
    ] {
        resolver
            .register(identity)
            .await
            .expect("register fixture identity");
    }
    services.agent_resolver = Arc::new(resolver);
    let services = Arc::new(services);
    let agent = ParentWatcherHandler {
        state,
        base_sha: fixture.base_sha.clone(),
        head_tree_sha: fixture.head_tree_sha.clone(),
        patch_digest: fixture.patch_digest.clone(),
        merge_tree_sha: fixture.merge_tree_sha.clone(),
    };
    let github = ParentGithubHandler {
        head_sha: fixture.head_sha.clone(),
    };
    test_runtime_builder_with_handlers(
        agent,
        github,
        MergePRHandler::new(services),
        ProcessHandler::new(),
    )
    .with_wasm_bytes(wasm_binary_bytes())
    .build()
    .await
    .expect("build real WASM guest with production MergePR handler")
}

fn owner_identity(
    name: &str,
    slug: &str,
    branch: &str,
    parent_branch: &str,
    slice_id: Option<&str>,
    working_dir: PathBuf,
) -> AgentIdentityRecord {
    AgentIdentityRecord {
        agent_name: AgentName::try_from_str(name).expect("agent identity name"),
        slug: Slug::try_from_str(slug).expect("agent identity slug"),
        agent_type: AgentType::Codex,
        birth_branch: BirthBranch::try_from_str(branch).expect("identity branch"),
        parent_branch: BirthBranch::try_from_str(parent_branch).expect("identity parent branch"),
        working_dir,
        display_name: format!("{name} controller"),
        topology: Topology::SharedDir,
        model: None,
        effort: None,
        ledger_owned: true,
        slice_id: slice_id.map(str::to_string),
    }
}

async fn mount_forgejo(server: &MockServer, state: Arc<ForgejoState>) {
    let pr_state = state.clone();
    Mock::given(method("GET"))
        .and(path(format!("/api/v1/repos/acme/repo/pulls/{PR_NUMBER}")))
        .respond_with(move |_request: &wiremock::Request| {
            let current_head = pr_state
                .forgejo_head_sha
                .lock()
                .expect("Forgejo head lock")
                .clone();
            let base_branch = pr_state
                .base_branch
                .lock()
                .expect("Forgejo base lock")
                .clone();
            let merged = pr_state.merged.load(Ordering::SeqCst);
            ResponseTemplate::new(200).set_body_json(json!({
                "number": PR_NUMBER,
                "title": "Aggregate architecture into main",
                "body": "Temporary isolated aggregate fixture",
                "state": if merged { "closed" } else { "open" },
                "merged": merged,
                "html_url": format!("http://forgejo.test/acme/repo/pulls/{PR_NUMBER}"),
                "head": {"ref": HEAD_BRANCH, "sha": current_head},
                "base": {"ref": base_branch, "sha": "fixture-base"},
            }))
        })
        .mount(server)
        .await;

    let ci_state = state.clone();
    Mock::given(method("GET"))
        .and(path(format!(
            "/api/v1/repos/acme/repo/commits/{}/status",
            state.head_sha
        )))
        .respond_with(move |_request: &wiremock::Request| {
            ResponseTemplate::new(200).set_body_json(json!({
                "state": ci_state.ci_status.lock().expect("CI lock").clone()
            }))
        })
        .mount(server)
        .await;

    Mock::given(method("POST"))
        .and(path(format!(
            "/api/v1/repos/acme/repo/pulls/{PR_NUMBER}/merge"
        )))
        .respond_with(move |_request: &wiremock::Request| {
            state.merge_calls.fetch_add(1, Ordering::SeqCst);
            let result = Command::new("git")
                .args(["--git-dir"])
                .arg(&state.bare_repository)
                .args(["update-ref", "refs/heads/main", &state.merge_head])
                .status();
            if !result.is_ok_and(|status| status.success()) {
                return ResponseTemplate::new(500);
            }
            state.merged.store(true, Ordering::SeqCst);
            match state.outcome {
                Outcome::Merge => ResponseTemplate::new(200).set_body_json(json!({"merged": true})),
                Outcome::CommitThenUnknownResponse => ResponseTemplate::new(500),
            }
        })
        .mount(server)
        .await;
}

fn forgejo_state(fixture: &Fixture, outcome: Outcome) -> Arc<ForgejoState> {
    Arc::new(ForgejoState {
        head_sha: fixture.head_sha.clone(),
        forgejo_head_sha: Mutex::new(fixture.head_sha.clone()),
        base_branch: Mutex::new(PARENT_BRANCH.to_string()),
        ci_status: Mutex::new("success".to_string()),
        merged: AtomicBool::new(false),
        merge_calls: AtomicUsize::new(0),
        bare_repository: fixture.bare_repository.clone(),
        merge_head: fixture.head_sha.clone(),
        outcome,
    })
}

async fn run_merge_tool(
    runtime: &exomonad_core::Runtime,
    fixture: &Fixture,
    reference: Value,
) -> Value {
    call_tool(
        runtime,
        "tl",
        "merge_pr",
        fixture.merge_arguments(reference),
    )
    .await
}

fn checkpoint_path(fixture: &Fixture) -> PathBuf {
    fixture
        .project_dir
        .join(".exo/tl-loop")
        .join(PARENT_RUN)
        .join("run.json")
}

fn write_checkpoint(fixture: &Fixture, checkpoint: &[u8]) {
    std::fs::write(checkpoint_path(fixture), checkpoint).expect("restore isolated parent state");
}

fn changed_policy_checkpoint(checkpoint: &[u8]) -> Vec<u8> {
    let mut state: Value = serde_json::from_slice(checkpoint).expect("parent state JSON");
    let manifest = state["plan_manifest"]
        .as_object_mut()
        .expect("manifest object");
    manifest["nodes"][0]["integration_contract"]["aggregate_approval_mode"] = json!("reviewer");
    let mut accepted_payload = manifest.clone();
    accepted_payload.remove("digest");
    manifest["digest"] = json!(digest(&Value::Object(accepted_payload)));
    serde_json::to_vec(&state).expect("updated policy checkpoint JSON")
}

fn changed_lineage_checkpoint(checkpoint: &[u8], reference: &mut Value) -> Vec<u8> {
    let mut state: Value = serde_json::from_slice(checkpoint).expect("parent state JSON");
    let candidate = &mut state["integration"]["candidates"][CHILD_SCOPE];
    let last_receipt = candidate["aggregate_approval_receipts"]
        .as_array()
        .expect("approval history")
        .len()
        - 1;
    let receipt = &mut candidate["aggregate_approval_receipts"][last_receipt];
    receipt["lineage_proof_digest"] = json!("a".repeat(64));
    let updated_id = receipt_id(receipt);
    receipt["receipt_id"] = json!(updated_id);
    candidate["active_aggregate_approval_id"] = json!(updated_id);
    reference["receipt_id"] = json!(updated_id);
    serde_json::to_vec(&state).expect("updated lineage checkpoint JSON")
}

fn changed_manifest_binding_checkpoint(checkpoint: &[u8], reference: &mut Value) -> Vec<u8> {
    let mut state: Value = serde_json::from_slice(checkpoint).expect("parent state JSON");
    let candidate = &mut state["integration"]["candidates"][CHILD_SCOPE];
    let history = candidate["aggregate_approval_receipts"]
        .as_array_mut()
        .expect("approval history");
    let receipt = history.last_mut().expect("active approval receipt");
    let forged_node = format!("{PARENT_RUN}/sub_tl/sibling");
    receipt["child_manifest_node_id"] = json!(forged_node);
    receipt["child_manifest_scope_id"] = json!(forged_node);
    let updated_id = receipt_id(receipt);
    receipt["receipt_id"] = json!(updated_id);
    candidate["active_aggregate_approval_id"] = json!(updated_id);
    reference["receipt_id"] = json!(updated_id);
    serde_json::to_vec(&state).expect("updated manifest-binding checkpoint JSON")
}

fn assert_merge_effect_rejected(output: &Value, label: &str) {
    let rejected = output["success"] == false || output["result"]["success"] == false;
    assert!(rejected, "{label} must block the merge effect: {output:#}");
}

fn digest(value: &Value) -> String {
    format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(value).expect("canonical JSON"))
    )
}

fn receipt_id(receipt: &Value) -> String {
    let mut identity = receipt.as_object().expect("receipt object").clone();
    identity.remove("receipt_id");
    identity.remove("created_at");
    digest(&Value::Object(identity))
}

fn git(directory: &Path, arguments: &[&str]) {
    let output = Command::new("git")
        .args(arguments)
        .current_dir(directory)
        .output()
        .expect("Git fixture command starts");
    assert!(
        output.status.success(),
        "git {arguments:?} failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn git_output(directory: &Path, arguments: &[&str]) -> String {
    let output = Command::new("git")
        .args(arguments)
        .current_dir(directory)
        .output()
        .expect("Git fixture command starts");
    assert!(
        output.status.success(),
        "git {arguments:?} failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout)
        .expect("Git output is UTF-8")
        .trim()
        .to_string()
}

fn git_dir_output(directory: &Path, arguments: &[&str]) -> String {
    let output = Command::new("git")
        .arg("--git-dir")
        .arg(directory)
        .args(arguments)
        .output()
        .expect("Git fixture command starts");
    assert!(
        output.status.success(),
        "git --git-dir {directory:?} {arguments:?} failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout)
        .expect("Git output is UTF-8")
        .trim()
        .to_string()
}

fn git_bytes(directory: &Path, arguments: &[&str]) -> Vec<u8> {
    let output = Command::new("git")
        .args(arguments)
        .current_dir(directory)
        .output()
        .expect("Git fixture command starts");
    assert!(
        output.status.success(),
        "git {arguments:?} failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    output.stdout
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn compiled_merge_tool_uses_production_handler_for_parent_approval() {
    let fixture = Fixture::new().await;
    let state = forgejo_state(&fixture, Outcome::Merge);
    let server = MockServer::start().await;
    mount_forgejo(&server, state.clone()).await;
    let forgejo = ForgejoClient::new(&server.uri(), "fixture-token").expect("Forgejo client");
    let runtime = build_production_merge_runtime(&fixture, forgejo, state.clone()).await;

    let superseded = run_merge_tool(&runtime, &fixture, fixture.superseded_reference.clone()).await;
    assert_tool_error(&superseded, "superseded A approval from A-B-A history");
    assert_eq!(state.merge_calls.load(Ordering::SeqCst), 0);

    let merged = run_merge_tool(&runtime, &fixture, fixture.reference.clone()).await;

    assert_tool_success(&merged, "merge_pr");
    assert_eq!(
        state.merge_calls.load(Ordering::SeqCst),
        1,
        "tool reported success without reaching the merge boundary: {merged:#}"
    );
    assert_eq!(
        git_dir_output(&fixture.bare_repository, &["rev-parse", "refs/heads/main"]),
        fixture.head_sha
    );

    let replay = run_merge_tool(&runtime, &fixture, fixture.reference.clone()).await;
    assert_tool_error(&replay, "merge_pr replay after merge");
    assert_eq!(state.merge_calls.load(Ordering::SeqCst), 1);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn compiled_merge_tool_does_not_repeat_an_unknown_merge_outcome() {
    let fixture = Fixture::new().await;
    let state = forgejo_state(&fixture, Outcome::CommitThenUnknownResponse);
    let server = MockServer::start().await;
    mount_forgejo(&server, state.clone()).await;
    let forgejo = ForgejoClient::new(&server.uri(), "fixture-token").expect("Forgejo client");
    let runtime = build_production_merge_runtime(&fixture, forgejo, state.clone()).await;

    let uncertain = run_merge_tool(&runtime, &fixture, fixture.reference.clone()).await;
    assert_eq!(
        uncertain["success"], true,
        "tool response envelope: {uncertain:#}"
    );
    assert_eq!(
        uncertain["result"]["success"], false,
        "merge result: {uncertain:#}"
    );
    assert!(state.merged.load(Ordering::SeqCst));
    assert_eq!(state.merge_calls.load(Ordering::SeqCst), 1);

    let replay = run_merge_tool(&runtime, &fixture, fixture.reference.clone()).await;
    assert_tool_error(&replay, "replay of already merged PR");
    assert_eq!(state.merge_calls.load(Ordering::SeqCst), 1);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn production_merge_boundary_rejects_stale_and_forged_parent_approval() {
    let fixture = Fixture::new().await;
    let state = forgejo_state(&fixture, Outcome::Merge);
    let server = MockServer::start().await;
    mount_forgejo(&server, state.clone()).await;
    let forgejo = ForgejoClient::new(&server.uri(), "fixture-token").expect("Forgejo client");
    let runtime = build_production_merge_runtime(&fixture, forgejo, state.clone()).await;
    let original_checkpoint = std::fs::read(checkpoint_path(&fixture)).expect("checkpoint bytes");

    let mut forged_receipt = fixture.reference.clone();
    forged_receipt["receipt_id"] = json!("a".repeat(64));
    let forged = run_merge_tool(&runtime, &fixture, forged_receipt).await;
    assert_merge_effect_rejected(&forged, "forged receipt reference");

    let mut wrong_parent = fixture.reference.clone();
    wrong_parent["parent_run_id"] = json!("unrelated-parent");
    let wrong_parent = run_merge_tool(&runtime, &fixture, wrong_parent).await;
    assert_merge_effect_rejected(&wrong_parent, "wrong parent run identity");

    let mut cross_scope = fixture.reference.clone();
    cross_scope["candidate_key"] = json!("sibling");
    let cross_scope = run_merge_tool(&runtime, &fixture, cross_scope).await;
    assert_merge_effect_rejected(&cross_scope, "cross-scope receipt reference");

    let mut stale_controller = fixture.reference.clone();
    stale_controller["controller_epoch"] = json!("epoch-previous");
    let stale_controller = run_merge_tool(&runtime, &fixture, stale_controller).await;
    assert_merge_effect_rejected(&stale_controller, "stale controller epoch");

    write_checkpoint(&fixture, &changed_policy_checkpoint(&original_checkpoint));
    let changed_policy = run_merge_tool(&runtime, &fixture, fixture.reference.clone()).await;
    assert_merge_effect_rejected(&changed_policy, "changed accepted policy");
    write_checkpoint(&fixture, &original_checkpoint);

    let mut manifest_reference = fixture.reference.clone();
    let invalid_manifest =
        changed_manifest_binding_checkpoint(&original_checkpoint, &mut manifest_reference);
    write_checkpoint(&fixture, &invalid_manifest);
    let mismatched_manifest = run_merge_tool(&runtime, &fixture, manifest_reference).await;
    assert_merge_effect_rejected(&mismatched_manifest, "forged canonical manifest binding");
    write_checkpoint(&fixture, &original_checkpoint);

    *state.forgejo_head_sha.lock().expect("Forgejo head lock") =
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".to_string();
    let changed_head = run_merge_tool(&runtime, &fixture, fixture.reference.clone()).await;
    assert_merge_effect_rejected(&changed_head, "changed Forgejo PR head");
    *state.forgejo_head_sha.lock().expect("Forgejo head lock") = fixture.head_sha.clone();

    *state.base_branch.lock().expect("Forgejo base lock") = "release".to_string();
    let changed_base = run_merge_tool(&runtime, &fixture, fixture.reference.clone()).await;
    assert_merge_effect_rejected(&changed_base, "changed Forgejo base branch");
    *state.base_branch.lock().expect("Forgejo base lock") = PARENT_BRANCH.to_string();

    *state.ci_status.lock().expect("Forgejo CI lock") = "failure".to_string();
    let failed_ci = run_merge_tool(&runtime, &fixture, fixture.reference.clone()).await;
    assert_merge_effect_rejected(&failed_ci, "failed authoritative CI");
    *state.ci_status.lock().expect("Forgejo CI lock") = "success".to_string();

    let mut lineage_reference = fixture.reference.clone();
    let changed_lineage = changed_lineage_checkpoint(&original_checkpoint, &mut lineage_reference);
    write_checkpoint(&fixture, &changed_lineage);
    let invalid_lineage = run_merge_tool(&runtime, &fixture, lineage_reference).await;
    assert_merge_effect_rejected(&invalid_lineage, "contradictory lineage receipt");

    assert_eq!(state.merge_calls.load(Ordering::SeqCst), 0);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial]
async fn production_merge_handler_rejects_a_worker_caller_for_parent_approval() {
    let fixture = Fixture::new().await;
    let state = forgejo_state(&fixture, Outcome::Merge);
    let server = MockServer::start().await;
    mount_forgejo(&server, state.clone()).await;
    let forgejo = ForgejoClient::new(&server.uri(), "fixture-token").expect("Forgejo client");
    let runtime = build_production_merge_runtime(&fixture, forgejo, state.clone()).await;
    let reference = exomonad_proto::effects::merge_pr::AggregateApprovalReference {
        receipt_id: fixture.receipt_id.clone(),
        parent_run_id: PARENT_RUN.to_string(),
        parent_scope_path: vec![PARENT_RUN.to_string()],
        candidate_key: CHILD_SCOPE.to_string(),
        controller_epoch: CONTROLLER_EPOCH.to_string(),
    };
    let request = exomonad_proto::effects::merge_pr::MergePrRequest {
        pr_number: PR_NUMBER as i64,
        strategy: "merge".to_string(),
        working_dir: fixture.working_dir.clone(),
        expected_base_sha: fixture.base_sha.clone(),
        expected_head_sha: fixture.head_sha.clone(),
        expected_patch_digest: fixture.patch_digest.clone(),
        expected_merge_tree_sha: fixture.merge_tree_sha.clone(),
        aggregate_approval: Some(reference),
    };
    let worker_context = exomonad_core::effects::EffectContext {
        agent_name: AgentName::try_from_str("worker").expect("worker name"),
        birth_branch: BirthBranch::try_from_str("main.worker").expect("worker branch"),
        working_dir: PathBuf::from("."),
    };

    let result = runtime
        .dispatch_effect(
            "merge_pr.merge_pr",
            &prost::Message::encode_to_vec(&request),
            &worker_context,
        )
        .await;

    assert!(
        result.is_err(),
        "worker identity cannot authorize parent approval"
    );
    assert_eq!(state.merge_calls.load(Ordering::SeqCst), 0);
}
