use super::*;
use crate::domain::{AgentName, BirthBranch, Slug};
use crate::services::agent_control::{AgentType, Topology};
use crate::services::agent_resolver::{AgentIdentityRecord, AgentResolver};
use exomonad_proto::effects::merge_pr::AggregateApprovalReference;
use serde_json::{json, Value};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

const REPOSITORY: &str = "https://forge.example.test/org/repo.git";

struct Fixture {
    directory: tempfile::TempDir,
    reference: AggregateApprovalReference,
    receipt: Value,
    repository: String,
    contract_mode: Option<String>,
}

impl Fixture {
    fn new() -> Self {
        let directory = tempfile::tempdir().expect("temporary project");
        let repository = REPOSITORY.to_string();
        let mut fixture = Self {
            directory,
            reference: AggregateApprovalReference {
                receipt_id: String::new(),
                parent_run_id: "root-run".to_string(),
                parent_scope_path: vec!["root-run".to_string()],
                candidate_key: "architecture".to_string(),
                controller_epoch: "epoch-current".to_string(),
            },
            receipt: Value::Null,
            repository,
            contract_mode: None,
        };
        fixture.receipt = fixture.receipt_document();
        let receipt_id = canonical_receipt_id(&fixture.receipt).expect("receipt ID");
        fixture.receipt["receipt_id"] = json!(receipt_id);
        fixture.reference.receipt_id = receipt_id;
        fixture.write_checkpoint();
        fixture
    }

    fn receipt_document(&self) -> Value {
        let contract = self.integration_contract();
        let plan_digest = canonical_digest(&self.manifest_payload()).expect("plan digest");
        let child_digest = self.child_manifest_digest();
        let policy = json!({
            "approval_mode": "parent",
            "parent_manifest_digest": plan_digest,
            "parent_manifest_revision": 2,
            "child_manifest_digest": child_digest,
            "integration_contract": contract,
        });
        json!({
            "receipt_id": "",
            "policy_mode": "parent",
            "parent_run_id": "root-run",
            "parent_scope_path": ["root-run"],
            "parent_manifest_scope_id": "root-run",
            "candidate_key": "architecture",
            "child_run_id": "architecture",
            "child_manifest_node_id": "root-run/sub_tl/architecture",
            "child_manifest_scope_id": "root-run/sub_tl/architecture",
            "parent_agent_id": "root",
            "parent_branch": "main",
            "repository_url": self.repository,
            "plan_manifest_digest": plan_digest,
            "plan_manifest_revision": 2,
            "approval_policy_digest": canonical_digest(&policy).expect("policy digest"),
            "child_manifest_digest": child_digest,
            "pr_number": 46,
            "head_sha": "cccccccccccccccccccccccccccccccccccccccc",
            "head_tree_sha": "dddddddddddddddddddddddddddddddddddddddd",
            "base_sha": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            "patch_digest": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
            "merge_tree_sha": "1111111111111111111111111111111111111111",
            "ci_status": "success",
            "readiness_receipt_digest": "2222222222222222222222222222222222222222222222222222222222222222",
            "lineage_proof_digest": "3333333333333333333333333333333333333333333333333333333333333333",
            "lineage_commit_shas": [
                "4444444444444444444444444444444444444444",
                "5555555555555555555555555555555555555555"
            ],
            "reviewed_leaf_ids": ["issue-slice"],
            "created_at": "2026-10-15T22:00:00Z",
            "supersedes_receipt_id": null,
        })
    }

    fn manifest_payload(&self) -> Value {
        let contract = self.integration_contract();
        let child_digest = self.child_manifest_digest();
        let child_manifest = self.child_manifest_document();
        let node = json!({
            "node_id": "root-run/sub_tl/architecture",
            "parent_id": "root-run",
            "kind": "sub_tl",
            "name": "architecture",
            "source_index": 0,
            "order": 1,
            "task": "Integrate architecture stage",
            "agent_type": "codex",
            "chainlink_issue_id": null,
            "boundary": ["src/architecture.rs"],
            "depends_on": [],
            "owned_branch": "main.architecture",
            "parent_integration_target": "main",
            "worktree": ".exo/worktrees/architecture",
            "declaration": {},
            "integration_contract": contract,
            "child_manifest_digest": child_digest,
        });
        json!({
            "schema_version": 1,
            "manifest_revision": 2,
            "scope_id": "root-run",
            "parent_scope_id": null,
            "role": "root",
            "owned_branch": "main",
            "parent_integration_target": null,
            "source_order": ["root-run/sub_tl/architecture"],
            "nodes": [node],
            "ordered_stages": [{"order": 1, "nodes": ["root-run/sub_tl/architecture"]}],
            "child_manifest_digests": {"root-run/sub_tl/architecture": child_digest},
            "child_manifests": {"root-run/sub_tl/architecture": child_manifest},
        })
    }

    fn child_manifest_payload(&self) -> Value {
        json!({
            "schema_version": 1,
            "manifest_revision": 2,
            "scope_id": "root-run/sub_tl/architecture",
            "parent_scope_id": "root-run",
            "role": "non_root",
            "owned_branch": "main.architecture",
            "nodes": [{
                "node_id": "issue-slice",
                "kind": "leaf",
                "name": "issue-slice",
            }],
        })
    }

    fn child_manifest_document(&self) -> Value {
        let mut manifest = self.child_manifest_payload();
        manifest["digest"] = json!(self.child_manifest_digest());
        manifest
    }

    fn child_manifest_digest(&self) -> String {
        canonical_digest(&self.child_manifest_payload()).expect("child manifest digest")
    }

    fn integration_contract(&self) -> Value {
        let mut contract = json!({
            "aggregate_pr_required": true,
            "aggregate_review_owner": "none",
            "base_revalidation_required": true,
            "leaf_review_owner": "leaf",
            "aggregate_repair_owner": "aggregate",
            "merge_strategy": "merge",
        });
        if let Some(mode) = self.contract_mode.as_ref() {
            contract["aggregate_approval_mode"] = json!(mode);
        }
        contract
    }

    fn refresh_receipt_and_checkpoint(&mut self) {
        self.receipt = self.receipt_document();
        let receipt_id = canonical_receipt_id(&self.receipt).expect("receipt ID");
        self.receipt["receipt_id"] = json!(receipt_id);
        self.reference.receipt_id = receipt_id;
        self.write_checkpoint();
    }

    fn manifest_document(&self) -> Value {
        let mut manifest = self.manifest_payload();
        let digest = canonical_digest(&manifest).expect("plan digest");
        manifest["digest"] = json!(digest);
        manifest
    }

    fn write_checkpoint(&self) {
        let receipt = self.receipt.clone();
        let manifest = self.manifest_document();
        let candidate = json!({
            "lifecycle": "MERGING",
            "aggregate_pr_number": 46,
            "aggregate_head_sha": receipt["head_sha"],
            "aggregate_patch_digest": receipt["patch_digest"],
            "aggregate_original_base_sha": receipt["base_sha"],
            "integration_owner_id": "root-run:architecture:integration",
            "integration_owner_run_id": "architecture",
            "integration_owner_branch": "main.architecture",
            "integration_owner_worktree": ".exo/worktrees/architecture",
            "head_sha": receipt["head_sha"],
            "patch_digest": receipt["patch_digest"],
            "validated_base_sha": receipt["base_sha"],
            "merge_tree_sha": receipt["merge_tree_sha"],
            "ci_status": "success",
            "aggregate_approval_receipts": [receipt.clone()],
            "active_aggregate_approval_id": receipt["receipt_id"],
        });
        let state = json!({
            "version": 5,
            "revision": 4,
            "run_id": "root-run",
            "depth": 0,
            "owner_branch": "main",
            "controller_epoch": "epoch-current",
            "repository_identity": {"remote_url": self.repository},
            "plan_manifest": manifest,
            "integration": {"candidates": {"architecture": candidate}},
        });
        fs::create_dir_all(self.directory.path().join(".exo/worktrees/architecture"))
            .expect("create aggregate owner worktree fixture");
        let scope = self.directory.path().join(".exo/tl-loop/root-run");
        fs::create_dir_all(&scope).expect("create TL scope");
        fs::write(
            scope.join(RUN_FILENAME),
            serde_json::to_vec(&state).expect("checkpoint JSON"),
        )
        .expect("write TL checkpoint");
    }

    async fn resolver(&self) -> AgentResolver {
        let resolver = AgentResolver::load(self.directory.path().to_path_buf()).await;
        resolver
            .register(AgentIdentityRecord {
                agent_name: AgentName::try_from_str("root").expect("agent name"),
                slug: Slug::try_from_str("root").expect("agent slug"),
                agent_type: AgentType::Codex,
                birth_branch: BirthBranch::try_from_str("main").expect("branch"),
                parent_branch: BirthBranch::try_from_str("main").expect("parent branch"),
                working_dir: Path::new(".").to_path_buf(),
                display_name: "root controller".to_string(),
                topology: Topology::SharedDir,
                model: None,
                effort: None,
                ledger_owned: true,
                slice_id: None,
            })
            .await
            .expect("register root controller identity");
        resolver
    }

    fn project_dir(&self) -> &Path {
        self.directory.path()
    }
}

fn production_python_artifact(request: Value) -> Value {
    let repository_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let generator = repository_root.join("tl_loop/tests/fixtures/aggregate_approval_producer.py");
    let python_venv = repository_root.join("tl_loop/.venv/bin/python");
    let python = if python_venv.exists() {
        python_venv
    } else {
        PathBuf::from("python3")
    };
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
    serde_json::from_slice(&output.stdout).expect("Python-generated approval artifact")
}

#[tokio::test]
async fn accepts_active_parent_receipt_for_declared_aggregate() {
    let fixture = Fixture::new();
    let resolver = fixture.resolver().await;
    let approval = validate_parent_approval(
        fixture.project_dir(),
        &fixture.reference,
        46,
        &fixture.repository,
        "root",
        "main",
        &resolver,
    )
    .await
    .expect("parent receipt should verify");

    assert_eq!(approval.receipt_id, fixture.reference.receipt_id);
    assert_eq!(approval.head_branch, "main.architecture");
    assert_eq!(approval.pr_number, 46);
    validate_request_compare(
        Some(&approval),
        &approval.base_sha,
        &approval.head_sha,
        &approval.patch_digest,
        &approval.merge_tree_sha,
    )
    .expect("request compare must bind the receipt");
}

#[tokio::test]
async fn accepts_python_produced_nested_scope_with_distinct_run_and_manifest_ids() {
    let directory = tempfile::tempdir().expect("temporary nested workspace");
    let project = directory.path();
    let scope_root = project.join(".exo/tl-loop");
    let parent_scope_id = "root-run/sub_tl/architecture";
    let artifact = production_python_artifact(json!({
        "parent_scope_path": ["root-run", "architecture"],
        "ancestor_child_branches": ["main.architecture"],
        "scope_root": scope_root,
        "root_branch": "main",
        "parent_run_id": "architecture",
        "parent_agent_id": "architecture",
        "parent_branch": "main.architecture",
        "child_run_id": "component",
        "child_branch": "main.architecture.component",
        "child_task": "Nested component work",
        "repository_url": REPOSITORY,
        "controller_epoch": "nested-epoch",
        "pr_number": 47,
        "head_sha": "c".repeat(40),
        "head_tree_sha": "d".repeat(40),
        "base_sha": "e".repeat(40),
        "patch_digest": "f".repeat(64),
        "merge_tree_sha": "1".repeat(40),
        "ci_status": "success",
        "readiness_receipt_digest": "2".repeat(64),
        "lineage_proof_digest": "3".repeat(64),
        "lineage_commit_shas": ["c".repeat(40)],
        "worktree": ".exo/worktrees/component",
    }));
    assert_eq!(artifact["parent_manifest_scope_id"], parent_scope_id);
    assert_eq!(
        artifact["child_manifest_node_id"],
        "root-run/sub_tl/architecture/sub_tl/component"
    );
    let reference = AggregateApprovalReference {
        receipt_id: artifact["receipt_id"].as_str().unwrap().to_string(),
        parent_run_id: "architecture".to_string(),
        parent_scope_path: vec!["root-run".to_string(), "architecture".to_string()],
        candidate_key: "component".to_string(),
        controller_epoch: "nested-epoch".to_string(),
    };
    for checkpoint in artifact["checkpoints"].as_array().unwrap() {
        let mut path = scope_root.clone();
        for segment in checkpoint["scope_path"].as_array().unwrap() {
            path.push(segment.as_str().unwrap());
        }
        fs::create_dir_all(&path).expect("create scope checkpoint directory");
        fs::write(
            path.join(RUN_FILENAME),
            serde_json::to_vec(&checkpoint["state"]).unwrap(),
        )
        .expect("write Python-generated checkpoint");
    }
    fs::create_dir_all(project.join(".exo/worktrees/component"))
        .expect("create nested aggregate worktree");
    let resolver = AgentResolver::load(project.to_path_buf()).await;
    resolver
        .register(AgentIdentityRecord {
            agent_name: AgentName::try_from_str("architecture").unwrap(),
            slug: Slug::try_from_str("architecture").unwrap(),
            agent_type: AgentType::Codex,
            birth_branch: BirthBranch::try_from_str("main.architecture").unwrap(),
            parent_branch: BirthBranch::try_from_str("main").unwrap(),
            working_dir: Path::new(".exo/worktrees/architecture").to_path_buf(),
            display_name: "nested parent controller".to_string(),
            topology: Topology::SharedDir,
            model: None,
            effort: None,
            ledger_owned: true,
            slice_id: Some(parent_scope_id.to_string()),
        })
        .await
        .expect("register nested parent identity");

    let approval = validate_parent_approval(
        project,
        &reference,
        47,
        REPOSITORY,
        "architecture",
        "main.architecture",
        &resolver,
    )
    .await
    .expect("Python-generated nested approval should validate");

    assert_eq!(approval.head_branch, "main.architecture.component");
    assert_eq!(approval.reference.parent_run_id, "architecture");
    assert_eq!(approval.reference.candidate_key, "component");
}

#[tokio::test]
async fn refuses_an_explicit_unsupported_aggregate_reviewer_policy() {
    let mut fixture = Fixture::new();
    fixture.contract_mode = Some("reviewer".to_string());
    fixture.refresh_receipt_and_checkpoint();
    let resolver = fixture.resolver().await;

    let error = validate_parent_approval(
        fixture.project_dir(),
        &fixture.reference,
        46,
        &fixture.repository,
        "root",
        "main",
        &resolver,
    )
    .await
    .unwrap_err();
    assert!(
        error
            .to_string()
            .contains("does not authorize parent aggregate approval mode"),
        "{error}"
    );
}

#[tokio::test]
async fn refuses_worker_sibling_and_publication_owner_contexts() {
    let fixture = Fixture::new();
    let resolver = fixture.resolver().await;
    for (agent, branch) in [("worker", "main.architecture"), ("root", "main.sibling")] {
        let error = validate_parent_approval(
            fixture.project_dir(),
            &fixture.reference,
            46,
            &fixture.repository,
            agent,
            branch,
            &resolver,
        )
        .await
        .unwrap_err();
        assert!(error.to_string().contains("caller"), "{error}");
    }
}

#[tokio::test]
async fn refuses_stale_epoch_repository_and_cross_scope_receipts() {
    let fixture = Fixture::new();
    let resolver = fixture.resolver().await;
    let mut stale = fixture.reference.clone();
    stale.controller_epoch = "epoch-old".to_string();
    assert!(validate_parent_approval(
        fixture.project_dir(),
        &stale,
        46,
        &fixture.repository,
        "root",
        "main",
        &resolver
    )
    .await
    .unwrap_err()
    .to_string()
    .contains("current RunStore"));

    assert!(validate_parent_approval(
        fixture.project_dir(),
        &fixture.reference,
        46,
        "https://forge.example.test/other/repo.git",
        "root",
        "main",
        &resolver,
    )
    .await
    .unwrap_err()
    .to_string()
    .contains("repository"));

    let mut sibling = fixture.reference.clone();
    sibling.candidate_key = "sibling".to_string();
    assert!(validate_parent_approval(
        fixture.project_dir(),
        &sibling,
        46,
        &fixture.repository,
        "root",
        "main",
        &resolver
    )
    .await
    .unwrap_err()
    .to_string()
    .contains("candidate"));
}

#[tokio::test]
async fn refuses_forged_or_superseded_receipts_and_changed_compare() {
    let fixture = Fixture::new();
    let resolver = fixture.resolver().await;
    let checkpoint = fixture.project_dir().join(".exo/tl-loop/root-run/run.json");
    let mut state: Value = serde_json::from_slice(&fs::read(&checkpoint).unwrap()).unwrap();
    state["integration"]["candidates"]["architecture"]["aggregate_approval_receipts"][0]
        ["head_sha"] = json!("9999999999999999999999999999999999999999");
    fs::write(&checkpoint, serde_json::to_vec(&state).unwrap()).unwrap();
    let forged = validate_parent_approval(
        fixture.project_dir(),
        &fixture.reference,
        46,
        &fixture.repository,
        "root",
        "main",
        &resolver,
    )
    .await
    .unwrap_err();
    assert!(forged.to_string().contains("ID does not match"), "{forged}");

    fixture.write_checkpoint();
    let approval = validate_parent_approval(
        fixture.project_dir(),
        &fixture.reference,
        46,
        &fixture.repository,
        "root",
        "main",
        &resolver,
    )
    .await
    .expect("fixture receipt should verify after reset");
    assert!(validate_request_compare(
        Some(&approval),
        &approval.base_sha,
        "9999999999999999999999999999999999999999",
        &approval.patch_digest,
        &approval.merge_tree_sha,
    )
    .unwrap_err()
    .to_string()
    .contains("head"));
}

#[test]
fn legacy_receipt_history_can_be_superseded_but_not_reactivated() {
    let fixture = Fixture::new();
    let checkpoint = fixture.project_dir().join(".exo/tl-loop/root-run/run.json");
    let mut state: Value = serde_json::from_slice(&fs::read(&checkpoint).unwrap()).unwrap();
    let mut legacy = fixture.receipt.clone();
    let old_candidate = legacy["candidate_key"].clone();
    for field in [
        "parent_manifest_scope_id",
        "candidate_key",
        "child_run_id",
        "child_manifest_node_id",
        "child_manifest_scope_id",
    ] {
        legacy.as_object_mut().unwrap().remove(field);
    }
    legacy["child_scope_id"] = old_candidate;
    legacy["receipt_id"] = json!("");
    let legacy_id = canonical_receipt_id(&legacy).unwrap();
    legacy["receipt_id"] = json!(legacy_id);
    let parsed_legacy = parse_receipt(&legacy).expect("old receipt remains decodable");
    assert!(parsed_legacy.legacy_identity);
    assert_eq!(parsed_legacy.candidate_key, "architecture");

    let mut current = fixture.receipt.clone();
    current["supersedes_receipt_id"] = json!(legacy_id);
    current["receipt_id"] = json!("");
    let current_id = canonical_receipt_id(&current).unwrap();
    current["receipt_id"] = json!(current_id);
    let candidate = &mut state["integration"]["candidates"]["architecture"];
    candidate["aggregate_approval_receipts"] = json!([legacy.clone(), current]);
    candidate["active_aggregate_approval_id"] = json!(current_id);
    fs::write(&checkpoint, serde_json::to_vec(&state).unwrap()).unwrap();
    let mut current_reference = fixture.reference.clone();
    current_reference.receipt_id = current_id.clone();
    let active = active_receipt(&state, &current_reference).expect("latest decision is active");
    assert_eq!(active.receipt_id, current_id);

    let mut legacy_only = state;
    let candidate = &mut legacy_only["integration"]["candidates"]["architecture"];
    candidate["aggregate_approval_receipts"] = json!([legacy]);
    candidate["active_aggregate_approval_id"] = json!(legacy_id);
    let mut old_reference = fixture.reference.clone();
    old_reference.receipt_id = legacy_id;
    let error = active_receipt(&legacy_only, &old_reference).unwrap_err();
    assert!(error.to_string().contains("must be renewed"), "{error}");
}

#[test]
fn refuses_aggregate_merge_when_the_reference_is_omitted() {
    let fixture = Fixture::new();
    let error =
        reject_unreferenced_aggregate(fixture.project_dir(), 46, &fixture.repository).unwrap_err();
    assert!(error
        .to_string()
        .contains("requires its active parent-approval receipt"));
}

#[test]
fn does_not_classify_a_leaf_candidate_as_an_aggregate() {
    let fixture = Fixture::new();
    let checkpoint = fixture.project_dir().join(".exo/tl-loop/root-run/run.json");
    let mut state: Value = serde_json::from_slice(&fs::read(&checkpoint).unwrap()).unwrap();
    state["plan_manifest"]["nodes"][0]["kind"] = json!("leaf");
    fs::write(&checkpoint, serde_json::to_vec(&state).unwrap()).unwrap();

    reject_unreferenced_aggregate(fixture.project_dir(), 46, &fixture.repository)
        .expect("leaf PRs remain governed by their authenticated review path");
}
