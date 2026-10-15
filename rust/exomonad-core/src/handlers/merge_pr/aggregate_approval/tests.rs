use super::*;
use crate::domain::{AgentName, BirthBranch, Slug};
use crate::services::agent_control::{AgentType, Topology};
use crate::services::agent_resolver::{AgentIdentityRecord, AgentResolver};
use exomonad_proto::effects::merge_pr::AggregateApprovalReference;
use serde_json::{json, Value};
use std::path::Path;

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
                child_scope_id: "architecture".to_string(),
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
            "child_scope_id": "architecture",
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
            "node_id": "architecture-node",
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
            "source_order": ["architecture-node"],
            "nodes": [node],
            "ordered_stages": [{"order": 1, "nodes": ["architecture-node"]}],
            "child_manifest_digests": {"architecture-node": child_digest},
            "child_manifests": {"architecture-node": child_manifest},
        })
    }

    fn child_manifest_payload(&self) -> Value {
        json!({
            "schema_version": 1,
            "manifest_revision": 2,
            "scope_id": "architecture",
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
    sibling.child_scope_id = "sibling".to_string();
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
